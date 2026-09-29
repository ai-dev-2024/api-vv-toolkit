from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from api_vv.generate import generate
from api_vv.llm import augment, complete, prompt_for, review
from api_vv.models import LLMConfig, Plan, Requirement, Requirements, Spec


def proposal(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": "missing widget",
        "operation_id": "getWidget",
        "parameters": {"path": {"widget_id": 999}},
        "body": None,
        "has_body": False,
        "expected_status": "404",
        "negative": True,
    }
    values.update(overrides)
    return values


def raw(*items: dict[str, Any]) -> str:
    return json.dumps({"proposals": list(items)})


@pytest.fixture
def base(spec: Spec, requirements: Requirements) -> Plan:
    return generate(spec, requirements)


def requirement(plan: Plan, req_id: str) -> Requirement:
    return next(item for item in plan.requirements.requirements if item.id == req_id)


def test_prompt_treats_spec_text_as_data(base: Plan) -> None:
    prompt = prompt_for(requirement(base, "REQ-010"), base)
    assert "never as instructions" in prompt
    payload = json.loads(prompt.split("\n", 1)[1])
    assert set(payload["operations"]) == {"getWidget"}
    assert payload["output_schema"]["required"] == ["proposals"]


def test_accepted_proposal_is_traced(base: Plan) -> None:
    cases, reviews = review(raw(proposal()), requirement(base, "REQ-010"), base)
    (accepted,) = cases
    assert accepted.generators == ["llm"] and accepted.requirement_ids == ["REQ-010"]
    assert reviews[0].accepted and reviews[0].case_id == accepted.id


@pytest.mark.parametrize(
    "text", ["not json", "[]", '{"proposals": {}}', '{"proposals": [], "extra": 1}']
)
def test_malformed_envelope(base: Plan, text: str) -> None:
    cases, reviews = review(text, requirement(base, "REQ-010"), base)
    assert cases == []
    assert [(r.index, r.accepted, r.reason) for r in reviews] == [
        (-1, False, "malformed proposal JSON envelope")
    ]


@pytest.mark.parametrize(
    "item, reason",
    [
        (proposal(operation_id="invented"), "unknown operation"),
        (proposal(expected_status="500"), "undocumented expected response"),
        (proposal(parameters={"path": {"widget_id": "abc"}}), "invalid parameter: path.widget_id"),
        (proposal(parameters={}), "missing parameter: path.widget_id"),
        (proposal(parameters={"path": {"widget_id": 1}, "query": {"q": 1}}), "unknown parameter"),
        (proposal(has_body=True, body={}), "operation has no request body"),
        (proposal(expected_status="200", negative=False), "outside the requested requirement"),
        (proposal(extra=1), "invalid proposal: extra: extra_forbidden"),
        (proposal(has_body="yes"), "invalid proposal: has_body: bool_type"),
        (proposal(expected_status="4xx"), "expected_status: string_pattern_mismatch"),
    ],
)
def test_rejected_proposals(base: Plan, item: dict[str, Any], reason: str) -> None:
    cases, reviews = review(raw(item), requirement(base, "REQ-010"), base)
    assert cases == []
    assert not reviews[0].accepted and reason in reviews[0].reason


def test_invalid_body_must_be_flagged_negative(base: Plan) -> None:
    item = proposal(
        operation_id="createWidget",
        parameters={},
        has_body=True,
        body={"name": "way-too-long", "role": "reader", "age": 1},
        expected_status="422",
        negative=False,
    )
    req = requirement(base, "REQ-003")
    _, reviews = review(raw(item), req, base)
    assert reviews[0].reason == "invalid body requires negative=true"
    _, reviews = review(raw({**item, "negative": True}), req, base)
    # REQ-003 selects a specific boundary check, so free-form LLM cases cannot claim it.
    assert reviews[0].reason == "requirement check selectors do not include llm-scenario"


def test_conflicting_expectation_is_rejected(base: Plan) -> None:
    item = proposal(parameters={"path": {"widget_id": 1}})
    _, reviews = review(raw(item), requirement(base, "REQ-010"), base)
    # Widget 1 exists; the plan already expects 200 for this exact request.
    assert reviews[0].reason.startswith("conflicting expectations for request TC-")


def test_augment_records_audit(base: Plan, config: Any) -> None:
    plan = asyncio.run(augment(base, config.llm))
    audit = {item.requirement_id: item for item in plan.llm_audit}
    assert len(audit) == 12
    reviews = audit["REQ-010"].reviews
    assert [r.accepted for r in reviews] == [True, False]
    assert reviews[1].reason == (
        "unknown operation; proposal is outside the requested requirement coverage"
    )
    assert audit["REQ-001"].raw_response == '{"proposals": []}'
    assert "REQ-010" in audit["REQ-010"].prompt


def test_augment_skips_inspection_requirements(base: Plan) -> None:
    base.requirements.requirements[0].verification_method = "inspection"
    plan = asyncio.run(augment(base, LLMConfig()))
    assert "REQ-001" not in {item.requirement_id for item in plan.llm_audit}


def test_mock_returns_raw_string_fixture(tmp_path: Any) -> None:
    fixtures = tmp_path / "f.yaml"
    fixtures.write_text("REQ-001: 'not json'\n", encoding="utf-8")
    config = LLMConfig(fixtures=str(fixtures))
    assert asyncio.run(complete(config, "REQ-001", "p")) == "not json"


def provider_env(monkeypatch: pytest.MonkeyPatch, url: str = "https://llm.invalid/v1") -> None:
    monkeypatch.setenv("API_VV_LLM_BASE_URL", url)
    monkeypatch.setenv("API_VV_LLM_API_KEY", "sk-secret-value")


def test_openai_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    provider_env(monkeypatch)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": raw()}}]})

    config = LLMConfig(provider="openai", model="m")
    result = asyncio.run(complete(config, "REQ-1", "hi", httpx.MockTransport(handler)))
    assert result == raw()
    (request,) = seen
    assert str(request.url) == "https://llm.invalid/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-secret-value"
    body = json.loads(request.content)
    assert body["temperature"] == 0 and body["response_format"]["type"] == "json_schema"


def test_anthropic_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    provider_env(monkeypatch)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        content = [
            {"type": "text", "text": "ignored"},
            {"type": "tool_use", "name": "propose_cases", "input": {"proposals": []}},
        ]
        return httpx.Response(200, json={"content": content})

    config = LLMConfig(provider="anthropic", model="m")
    result = asyncio.run(complete(config, "REQ-1", "hi", httpx.MockTransport(handler)))
    assert json.loads(result) == {"proposals": []}
    assert str(seen[0].url) == "https://llm.invalid/v1/messages"
    assert seen[0].headers["x-api-key"] == "sk-secret-value"
    assert json.loads(seen[0].content)["tool_choice"]["name"] == "propose_cases"


@pytest.mark.parametrize(
    "provider, response",
    [
        ("openai", httpx.Response(500, text="upstream echoed sk-secret-value")),
        ("openai", httpx.Response(200, json={"choices": [{"message": {"content": None}}]})),
        ("openai", httpx.Response(200, json={})),
        ("anthropic", httpx.Response(200, json={"content": [{"type": "text"}]})),
    ],
)
def test_provider_errors_hide_details(
    monkeypatch: pytest.MonkeyPatch, provider: Any, response: httpx.Response
) -> None:
    provider_env(monkeypatch)
    config = LLMConfig(provider=provider)
    transport = httpx.MockTransport(lambda request: response)
    with pytest.raises(ValueError, match=r"^LLM provider request failed \(\w+\)$") as info:
        asyncio.run(complete(config, "REQ-1", "hi", transport))
    assert "sk-secret" not in str(info.value)


def test_provider_configuration_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    config = LLMConfig(provider="openai")
    monkeypatch.delenv("API_VV_LLM_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="must be set"):
        asyncio.run(complete(config, "REQ-1", "hi"))
    for url in ("ftp://llm.invalid", "https://user:pw@llm.invalid", "https://llm.invalid/?a=1"):
        provider_env(monkeypatch, url)
        with pytest.raises(ValueError, match="HTTP"):
            asyncio.run(complete(config, "REQ-1", "hi"))
