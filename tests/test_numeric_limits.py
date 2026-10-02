"""Reject non-finite budgets before they reach async timeout or verdict logic."""

import pytest
from pydantic import ValidationError

from api_vv.models import Config, LLMConfig, Requirement


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
@pytest.mark.parametrize("model", [Config, LLMConfig])
def test_timeouts_must_be_finite(model: type[Config] | type[LLMConfig], value: float) -> None:
    with pytest.raises(ValidationError):
        model(timeout_seconds=value)


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_latency_budget_must_be_finite(value: float) -> None:
    with pytest.raises(ValidationError):
        Requirement.model_validate(
            {
                "id": "REQ-001",
                "text": "Bounded response time",
                "priority": "must",
                "verification_method": "test",
                "latency_ms": value,
                "covers": [{"operation_id": "health", "responses": ["200"]}],
            }
        )


def test_finite_budgets_are_preserved() -> None:
    assert Config(timeout_seconds=0.25).timeout_seconds == 0.25
    assert LLMConfig(timeout_seconds=30).timeout_seconds == 30
