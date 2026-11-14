import pytest
from pydantic import ValidationError
from app.v2.domain import Policy, credit_eligible, sanction_for
from app.v2.llm import Verdict, request_body, CheckError
from app.v2.content import normalize, extract
from app.v2.operator import parse_import, csv_safe
from app.v2.onboarding import rights
from conftest import message


def test_policy_preserves_five_and_validates_thresholds():
    assert Policy().auto_whitelist_threshold == 5
    for threshold in [0, 1001]:
        with pytest.raises(ValidationError):
            Policy(auto_whitelist_threshold=threshold)
    with pytest.raises(ValidationError):
        Policy(warn_at=5, mute_at=4)
    assert [sanction_for(n, Policy()) for n in [1, 2, 4, 5, 6]] == [
        "warn",
        None,
        "mute",
        "ban",
        "ban",
    ]


@pytest.mark.parametrize(
    "model,human,complete,expected",
    [
        ("OK", None, True, True),
        ("OK", None, False, False),
        ("OK", "SCAM", True, False),
        ("SCAM", "NOT_SCAM", False, True),
        (None, None, False, False),
    ],
)
def test_credit_precedence(model, human, complete, expected):
    assert credit_eligible(model, human, complete) is expected


def test_classifier_refuses_ambiguous_json():
    with pytest.raises(ValidationError):
        Verdict.model_validate_json('{"label":"OK"}')
    with pytest.raises(CheckError):
        request_body("unknown", {})
    assert "reasoning_effort" not in request_body("gpt-4o-mini", {})
    assert request_body("gpt-6-luna", {})["reasoning_effort"] == "none"


def test_quote_is_not_publication_and_x_not_changed():
    original = message(1, text="Offer")
    reply = message(2, text="This is a scam; do not pay", reply_to_message=original)
    content = extract(reply)
    assert content["own"] == reply.text
    assert content["quoted_context"] == "Offer"
    assert normalize("x e\u200bxample.com") == "x example.com"


def test_csv_formula_and_import_validation():
    assert csv_safe(" =HYPERLINK('x')").startswith("'")
    assert len(parse_import(b"telegram_user_id,reason\n12,one\n12,two\n")) == 1
    with pytest.raises(Exception):
        parse_import(b"telegram_user_id\n-123\n")


def test_chat_selection_rights_superset():
    for side in ["work", "admin"]:
        user, bot = rights(side, True).model_dump(), rights(side).model_dump()
        assert all(not value or user[key] for key, value in bot.items())
