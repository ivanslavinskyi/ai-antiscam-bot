import httpx
import pytest
from pydantic import ValidationError

from app.v2.config import Settings
from app.v2.llm import Classifier, CheckError, RetryLater
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
