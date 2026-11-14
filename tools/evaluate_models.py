"""Offline release gate for independently reviewed model evaluation data.

This command sends no data to a provider and does not load .env. Predictions must
be generated separately on an explicitly approved, minimized evaluation dataset.
"""

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re


def read_jsonl(path):
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def fingerprint(text):
    normalized = re.sub(r"\s+", " ", text.casefold()).strip()
    normalized = re.sub(r"https?://\S+", "<url>", normalized)
    normalized = re.sub(r"\b\d+\b", "<number>", normalized)
    return normalized


def shingles(text):
    value = fingerprint(text)
    return {value[i : i + 5] for i in range(max(1, len(value) - 4))}


def validate_dataset(rows):
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate example IDs")
    for row in rows:
        if (
            row.get("label") not in {"SCAM", "NOT_SCAM"}
            or not row.get("reviewed_by")
            or row.get("review_source") != "independent_human"
        ):
            raise ValueError(
                "Every example requires an independent human label and reviewer"
            )
        if row.get("split") not in {"tuning", "holdout"} or not row.get("text"):
            raise ValueError("Each example needs text and an explicit split")
        if not row.get("near_duplicate_group"):
            raise ValueError("Assign near_duplicate_group before splitting")
    tuning = [r for r in rows if r["split"] == "tuning"]
    holdout = [r for r in rows if r["split"] == "holdout"]
    counts = Counter(r["label"] for r in holdout)
    if counts["SCAM"] < 200 or counts["NOT_SCAM"] < 300 or not tuning:
        raise ValueError(
            "Need separate tuning data and holdout with >=200 SCAM and >=300 NOT_SCAM"
        )
    tuning_groups = {r["near_duplicate_group"] for r in tuning}
    if any(r["near_duplicate_group"] in tuning_groups for r in holdout):
        raise ValueError("Near-duplicate group crosses tuning and holdout")
    reference = [(r["id"], shingles(r["text"])) for r in tuning]
    for row in holdout:
        source = shingles(row["text"])
        for other, grams in reference:
            if min(len(source), len(grams)) / max(len(source), len(grams), 1) < 0.8:
                continue
            if len(source & grams) / max(len(source | grams), 1) >= 0.8:
                raise ValueError(
                    f"Possible text leakage between {row['id']} and {other}; review split"
                )
    mandatory = {
        "quote",
        "reply",
        "legitimate_job",
        "ukrainian",
        "russian",
        "url",
        "ocr",
        "prompt_injection",
    }
    if not mandatory <= {tag for r in holdout for tag in r.get("tags", [])}:
        raise ValueError("Required regression categories are missing from holdout")
    protected = {
        tag
        for r in holdout
        if r.get("must_protect_author") and r["label"] == "NOT_SCAM"
        for tag in r.get("tags", [])
    }
    if not {"quote", "reply", "legitimate_job"} <= protected:
        raise ValueError(
            "Protected innocent-author cases are required for quotes, replies and legitimate jobs"
        )
    return holdout


def wilson(successes, total):
    if not total:
        return None
    z = 1.959963984540054
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    delta = (
        z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    )
    return [max(0.0, centre - delta), min(1.0, centre + delta)]


def metrics(holdout, predictions, threshold=0.8):
    by_id = {r["id"]: r for r in predictions}
    if len(by_id) != len(predictions) or set(by_id) != {r["id"] for r in holdout}:
        raise ValueError("Predictions must cover each holdout example exactly once")
    tp = fp = fn = tn = errors = 0
    cost = 0.0
    missing_cost = 0
    mandatory_fail = []
    for row in holdout:
        prediction = by_id[row["id"]]
        if not prediction.get("error") and (
            prediction.get("label") not in {"SCAM", "OK"}
            or not isinstance(prediction.get("confidence"), (int, float))
            or not 0 <= prediction["confidence"] <= 1
        ):
            raise ValueError("Invalid prediction label or confidence")
        value = prediction.get("cost_usd")
        if value is not None and (
            not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0
        ):
            raise ValueError("Cost must be a finite, nonnegative amount or null")
        predicted = (
            prediction.get("label") == "SCAM"
            and prediction.get("confidence", 0) >= threshold
            and not prediction.get("error")
        )
        actual = row["label"] == "SCAM"
        tp += bool(predicted and actual)
        fp += bool(predicted and not actual)
        fn += bool(not predicted and actual)
        tn += bool(not predicted and not actual)
        errors += bool(prediction.get("error"))
        if prediction.get("cost_usd") is None:
            missing_cost += 1
        else:
            cost += float(prediction["cost_usd"])
        if row.get("must_protect_author") and (predicted or prediction.get("error")):
            mandatory_fail.append(row["id"])
    return {
        "false_positive_rate": fp / max(1, fp + tn),
        "recall": tp / max(1, tp + fn),
        "precision": tp / max(1, tp + fp),
        "errors": errors,
        "cost_usd": cost,
        "missing_cost": missing_cost,
        "mandatory_failures": mandatory_fail,
        "counts": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "confidence_intervals_95": {
            "false_positive_rate": wilson(fp, fp + tn),
            "recall": wilson(tp, tp + fn),
            "precision": wilson(tp, tp + fp),
        },
    }


def compare(holdout, baseline, candidate):
    old, new = metrics(holdout, baseline), metrics(holdout, candidate)
    checks = {
        "innocent_author": not new["mandatory_failures"],
        "false_positives": new["false_positive_rate"]
        <= old["false_positive_rate"] + 0.01,
        "recall": new["recall"] >= old["recall"] - 0.02,
        "cost": not old["missing_cost"]
        and not new["missing_cost"]
        and new["cost_usd"] <= old["cost_usd"],
        "complete_responses": new["errors"] == old["errors"] == 0,
    }
    return {
        "baseline": old,
        "candidate": new,
        "gates": checks,
        "passed": all(checks.values()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    holdout = validate_dataset(read_jsonl(args.dataset))
    report = compare(holdout, read_jsonl(args.baseline), read_jsonl(args.candidate))
    report["dataset_sha256"] = hashlib.sha256(args.dataset.read_bytes()).hexdigest()
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("Model release gate:", "PASS" if report["passed"] else "FAIL")
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
