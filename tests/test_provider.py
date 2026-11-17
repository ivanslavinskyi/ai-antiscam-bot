import json

import httpx
import pytest
from pydantic import ValidationError

from app.v2.config import Settings
from app.v2.llm import Classifier, CheckError, RetryLater, cost, input_bounds, request_body
from tools.evaluate_models import metrics, wilson


class RecordingBudget:
    def __init__(self):
        self.reservations, self.settlements = [], []

    async def reserve(self, *args):
        self.reservations.append(args)

    async def settle(self, *args):
        self.settlements.append(args)


@pytest.mark.parametrize(
    "status,body,error,spent",
    [
        (429, {}, RetryLater, 0),
        (500, {}, CheckError, None),
        (
            200,
            {
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "not JSON"}}
                ],
            },
            CheckError,
            "known",
        ),
    ],
)
async def test_provider_failures_are_not_safe_results(status, body, error, spent):
    budget = RecordingBudget()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status, json=body, headers={"retry-after": "7"}
            )
        )
    )
    classifier = Classifier(
        Settings(
            _env_file=None, db_url="postgresql://unused", openai_api_key="test-fixture"
        ),
        budget,
        client,
    )
    try:
        with pytest.raises(error):
            await classifier.classify(1, "attempt-1", {"own_publication": ["fixture"]})
    finally:
        await classifier.close()
    assert len(budget.reservations) == len(budget.settlements) == 1
    charge = budget.settlements[0][1]
    assert charge > 0 if spent == "known" else charge == spent


async def test_transport_timeout_reserves_unknown_cost():
    def timeout(request):
        raise httpx.ReadTimeout("fixture timeout", request=request)

    budget = RecordingBudget()
    client = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
    classifier = Classifier(
        Settings(
            _env_file=None, db_url="postgresql://unused", openai_api_key="test-fixture"
        ),
        budget,
        client,
    )
    try:
        with pytest.raises(CheckError, match="provider_transport"):
            await classifier.classify(1, "attempt-1", {"own_publication": ["fixture"]})
    finally:
        await classifier.close()
    assert budget.settlements == [("attempt-1",)]


def test_evaluation_rejects_invalid_predictions_and_negative_cost():
    truth = [{"id": "one", "label": "NOT_SCAM"}]
    for prediction in [
        {"id": "one", "label": "UNKNOWN", "confidence": 1, "cost_usd": 0},
        {"id": "one", "label": "OK", "confidence": 1, "cost_usd": -1},
    ]:
        with pytest.raises(ValueError):
            metrics(truth, [prediction])
    low, high = wilson(0, 300)
    assert low == pytest.approx(0) and 0 < high < 0.02


def test_configuration_error_does_not_echo_secrets():
    with pytest.raises(ValidationError) as error:
        Settings(
            _env_file=None,
            db_url="postgresql://fixture-secret",
            openai_api_key="fixture-secret",
            worker_count="fixture-secret",
        )
    assert "fixture-secret" not in str(error.value)


@pytest.mark.parametrize(
    "details,expected",
    [
        ({"cache_write_tokens": 0}, 0.00015),
        ({"cache_write_tokens": 400}, 0.00016),
        ({"cache_write_tokens": 1000}, 0.000175),
        ({"cache_write_tokens": 400, "cached_tokens": 500}, 0.00016),
        (None, 0.000175),
        ({}, 0.000175),
        ([], 0.000175),
        ({"cache_write_tokens": -1}, 0.000175),
        ({"cache_write_tokens": 1001}, 0.000175),
        ({"cache_write_tokens": True}, 0.000175),
        ({"cache_write_tokens": "400"}, 0.000175),
    ],
)
async def test_luna_cache_write_accounting_and_reservation(details, expected):
    budget = RecordingBudget()

    def respond(request):
        body = json.loads(request.content)
        assert body["model"] == "gpt-6-luna"
        assert body["reasoning_effort"] == "none"
        # This tariff is for the default 30-minute cache, not extended retention.
        assert "prompt_cache_options" not in body
        return httpx.Response(
            200,
            json={
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 100,
                    "prompt_tokens_details": details,
                },
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "label": "OK",
                                    "category": "other",
                                    "confidence": 0.9,
                                    "reason": "fixture",
                                }
                            )
                        },
                    }
                ],
            },
        )

    classifier = Classifier(
        Settings(
            _env_file=None,
            db_url="postgresql://unused",
            openai_api_key="test-fixture",
            openai_model="gpt-6-luna",
            max_input_tokens=16000,
        ),
        budget,
        httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    try:
        verdict, meta = await classifier.classify(
            1,
            "luna-write",
            {
                "own_publication": ["fixture " * 1000],
            },
        )
    finally:
        await classifier.close()
    assert verdict.label == "OK"
    assert meta["cost_usd"] == pytest.approx(expected)
    assert budget.settlements[0][1] == pytest.approx(expected)
    # Even if every input token requires a cache write, reservation covers it.
    assert budget.reservations[0][2] >= (8000 * 0.125 + 512 * 0.50) / 1_000_000
    assert budget.reservations[0][2] > expected


def test_legacy_model_cost_is_unchanged():
    for writes in (None, 0, 1000):
        assert cost(
            "gpt-4o-mini", 1000, 100, cache_write_tokens=writes
        ) == pytest.approx(0.00021)


@pytest.mark.parametrize("character", ["я", "界", "😀"])
async def test_full_unicode_publication_and_context_reach_provider_intact(character):
    content = {
        "own_publication": [character * 4096 + " END_OF_PUBLICATION"],
        "community_context": "Описание сообщества. " * 95,
        "publication_context": [{"reply": {"text": "Цитата. " * 512}}],
        "links": ["https://example.org/fixture"],
    }
    budget = RecordingBudget()
    calls = []

    def respond(request):
        body = json.loads(request.content)
        assert json.loads(body["messages"][1]["content"]) == content
        calls.append(body)
        return httpx.Response(200, json={
            "usage": {"prompt_tokens": 5000, "completion_tokens": 40},
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                "label": "OK", "category": "other", "confidence": 0.95,
                "reason": "fixture",
            })}}],
        })

    settings = Settings(_env_file=None, db_url="postgresql://unused",
                        openai_api_key="fixture", openai_model="gpt-6-luna")
    classifier = Classifier(settings, budget, httpx.AsyncClient(
        transport=httpx.MockTransport(respond)))
    try:
        verdict, meta = await classifier.classify(1, "long", content)
    finally:
        await classifier.close()
    assert len(calls) == 1 and verdict.label == "OK"
    chars, upper = input_bounds(calls[0])
    assert chars <= settings.max_input_chars and 8000 < upper <= settings.max_input_tokens
    assert budget.reservations[0][2] == cost("gpt-6-luna", upper, 512)
    assert budget.settlements == [("long", meta["cost_usd"])]
    assert meta["cost_usd"] < budget.reservations[0][2]


@pytest.mark.parametrize("limits,content", [
    ({}, {"own_publication": ["x" * 16000]}),
    ({"max_input_tokens": 8000}, {"own_publication": ["я" * 4096]}),
    ({"max_input_chars": 1000}, {"own_publication": ["hello"]}),
])
async def test_oversize_or_explicit_lower_limits_reject_without_call_or_reservation(limits, content):
    def unexpected(request):
        pytest.fail("Oversized request must not call the provider")

    budget = RecordingBudget()
    classifier = Classifier(Settings(_env_file=None, db_url="postgresql://unused",
        openai_api_key="fixture", **limits), budget,
        httpx.AsyncClient(transport=httpx.MockTransport(unexpected)))
    try:
        with pytest.raises(CheckError, match="^input_limit$"):
            await classifier.classify(1, "oversize", content)
    finally:
        await classifier.close()
    assert budget.reservations == budget.settlements == []


def test_default_byte_ceiling_covers_full_character_budget_including_schema():
    settings = Settings(_env_file=None, db_url="postgresql://unused")
    body = request_body("gpt-6-luna", {})
    # Worst-case UTF-8 text at the whole-message character limit.
    body["messages"] = [{"role": "user", "content": ""}]
    overhead = len(json.dumps(body["messages"], ensure_ascii=False))
    body["messages"][0]["content"] = "😀" * (settings.max_input_chars - overhead)
    chars, upper = input_bounds(body)
    assert chars == settings.max_input_chars
    assert upper <= settings.max_input_tokens
