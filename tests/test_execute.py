from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from api_vv.execute import execute, request_parts, validate_plan, validate_response
from api_vv.models import Case, Config, Plan, Response, Result, SecurityScheme
from api_vv.report import gate_failures, matrix


def results_by_requirement(plan: Plan, results: list[Result]) -> dict[str, set[str]]:
    verdicts = {result.case_id: result.verdict for result in results}
    found: dict[str, set[str]] = {}
    for case in plan.cases:
        for req in case.requirement_ids:
            found.setdefault(req, set()).add(verdicts[case.id])
    return found


def test_demo_run_catches_exactly_the_seeded_defects(
    plan: Plan, config: Config, transport: httpx.ASGITransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_VV_DEMO_KEY", "fixture-secret")
    run = asyncio.run(execute(plan, "http://demo.test", config, transport))
    failed = {r.case_id: r for r in run.results if r.verdict == "failed"}
    assert len(failed) == 2
    by_req = results_by_requirement(plan, run.results)
    assert by_req["REQ-003"] == {"failed"} and by_req["REQ-008"] == {"failed"}
    messages = sorted(error for result in failed.values() for error in result.errors)
    assert messages == [
        "response status is undocumented",
        "status: expected 204, got 200",
        "status: expected 422, got 201",
    ]
    assert gate_failures(matrix(plan, run), config) == ["REQ-003", "REQ-008"]


def response(status: int, **kwargs: Any) -> httpx.Response:
    return httpx.Response(status, **kwargs)


def check(result: httpx.Response, case: Case, plan: Plan) -> list[str]:
    return validate_response(result, case, plan.spec.operations[case.operation_id])


def test_response_validation(plan: Plan) -> None:
    health = next(c for c in plan.cases if c.operation_id == "health")
    version = {"X-Service-Version": "1.0"}
    assert check(response(200, json={"status": "ok"}, headers=version), health, plan) == []
    assert check(response(200, json={"status": "bad", "x": 1}), health, plan) == [
        "missing response header: X-Service-Version",
        "body schema: / [additionalProperties]",
        "body schema: /status [const]",
    ]
    wrong_header = response(200, json={"status": "ok"}, headers={"X-Service-Version": "2"})
    assert check(wrong_header, health, plan) == ["header X-Service-Version schema: / [const]"]
    broken = response(200, text="{", headers={**version, "content-type": "application/json"})
    assert check(broken, health, plan) == ["response body is not valid JSON"]
    assert check(response(200, text="ok", headers=version), health, plan) == [
        "response content-type is missing or undocumented"
    ]
    delete = next(c for c in plan.cases if c.operation_id == "deleteWidget")
    assert check(response(204, text="x"), delete, plan) == ["response body is undocumented"]
    assert check(response(500), delete, plan) == [
        "status: expected 204, got 500",
        "response status is undocumented",
    ]


def test_wildcard_and_text_responses(plan: Plan) -> None:
    op = plan.spec.operations["health"].model_copy(deep=True)
    del op.responses["200"]
    op.responses["2XX"] = Response.model_validate(
        {
            "content": {"text/*": {"type": "string", "maxLength": 2}},
            "headers": {"X-Count": {"schema": {"type": "integer"}}},
        }
    )
    op.responses["default"] = Response()
    case = Case(
        id="c",
        name="c",
        operation_id="health",
        expected_status="2XX",
        generators=["t"],
        checks=["t"],
    )
    result = response(201, text="abc", headers={"content-type": "text/plain", "X-Count": "x"})
    assert validate_response(result, case, op) == [
        "header X-Count schema: / [type]",
        "body schema: / [maxLength]",
    ]
    case.expected_status = "default"
    assert validate_response(response(503), case, op) == []


def test_request_parts_and_auth(
    plan: Plan, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = plan.spec.model_copy(deep=True)
    op = spec.operations["secure"].model_copy(deep=True)
    case = Case(
        id="c",
        name="c",
        operation_id="secure",
        expected_status="200",
        parameters={
            "query": {"flag": True, "none": None},
            "cookie": {"c": 1},
            "header": {"X-A": 2},
        },
        generators=["t"],
        checks=["t"],
    )
    monkeypatch.delenv("API_VV_DEMO_KEY")
    with pytest.raises(ValueError, match="missing configured credentials"):
        request_parts(case, op, spec, config)
    monkeypatch.setenv("API_VV_DEMO_KEY", "k")
    path, headers, query, cookies = request_parts(case, op, spec, config)
    assert (path, headers, query, cookies) == (
        "/secure",
        {"x-a": "2", "x-api-key": "k"},
        {"flag": "true", "none": ""},
        {"c": "1"},
    )
    case.auth = "omit"
    case.parameters["header"]["X-API-Key"] = "leftover"
    assert "x-api-key" not in request_parts(case, op, spec, config)[1]
    for scheme, expected in (
        (SecurityScheme(type="http", scheme="basic"), "Basic dTpw"),
        (SecurityScheme(type="http", scheme="bearer"), "Bearer u:p"),
        (SecurityScheme(type="oauth2"), "Bearer u:p"),
    ):
        spec.security_schemes["DemoKey"] = scheme
        monkeypatch.setenv("API_VV_DEMO_KEY", "u:p")
        case.auth = "configured"
        assert request_parts(case, op, spec, config)[1]["authorization"] == expected
        case.auth = "omit"
        case.parameters["header"]["Authorization"] = "x"
        assert "authorization" not in request_parts(case, op, spec, config)[1]
        del case.parameters["header"]["Authorization"]
    spec.security_schemes["DemoKey"] = SecurityScheme(type="http", scheme="digest")
    case.auth = "configured"
    with pytest.raises(ValueError, match="unsupported authentication"):
        request_parts(case, op, spec, config)
    widget = spec.operations["getWidget"]
    unfilled = case.model_copy(update={"operation_id": "getWidget", "parameters": {}})
    with pytest.raises(ValueError, match="unfilled"):
        request_parts(unfilled, widget, spec, config)
    filled = unfilled.model_copy(update={"parameters": {"path": {"widget_id": "a/b"}}})
    assert request_parts(filled, widget, spec, config)[0] == "/widgets/a%2Fb"


def health_plan(plan: Plan) -> Plan:
    subset = plan.model_copy(deep=True)
    subset.cases = [c for c in subset.cases if c.operation_id == "health"]
    return subset


def test_retries_only_transport_errors_on_safe_methods(plan: Plan) -> None:
    calls = 0

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"status": "ok"}, headers={"X-Service-Version": "1.0"})

    subset = health_plan(plan)
    run = asyncio.run(
        execute(subset, "http://h.test", Config(retries=1), httpx.MockTransport(flaky))
    )
    assert [(r.verdict, r.attempts) for r in run.results] == [("passed", 2)]
    calls = 0
    run = asyncio.run(execute(subset, "http://h.test", Config(), httpx.MockTransport(flaky)))
    assert run.results[0].errors == ["request failed (ConnectError)"]


def test_latency_budget_and_disabled_cases(plan: Plan, monkeypatch: pytest.MonkeyPatch) -> None:
    subset = health_plan(plan)
    subset.requirements.requirements[11].latency_ms = 0.000001
    disabled = subset.cases[0].model_copy(update={"id": "TC-off", "enabled": False})
    subset.cases.append(disabled)

    def slow(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"}, headers={"X-Service-Version": "1.0"})

    run = asyncio.run(execute(subset, "http://h.test", Config(), httpx.MockTransport(slow)))
    assert run.results[0].errors == ["REQ-012: latency exceeds 1e-06 ms"]
    assert run.results[1].verdict == "skipped" and run.results[1].errors == ["disabled"]


def test_missing_credentials_fail_the_case(plan: Plan, monkeypatch: pytest.MonkeyPatch) -> None:
    subset = plan.model_copy(deep=True)
    subset.cases = [c for c in subset.cases if c.operation_id == "secure" and c.auth != "omit"]
    monkeypatch.delenv("API_VV_DEMO_KEY", raising=False)
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    run = asyncio.run(execute(subset, "http://h.test", Config(), transport))
    assert run.results[0].errors == [
        "missing configured credentials for operation security requirements"
    ]


@pytest.mark.parametrize(
    "url", ["ftp://h.test", "http://u:p@h.test", "http://h.test/?q=1", "http://h.test/#f", "/x"]
)
def test_base_url_is_validated(plan: Plan, url: str) -> None:
    with pytest.raises(ValueError, match="base URL"):
        asyncio.run(execute(plan, url))


def test_plan_integrity_checks(plan: Plan) -> None:
    edited = plan.model_copy(deep=True)
    edited.cases[0].requirement_ids = ["REQ-002"]
    with pytest.raises(ValueError, match="trace links disagree"):
        validate_plan(edited)
    edited = plan.model_copy(deep=True)
    edited.cases[0].expected_status = "599"
    with pytest.raises(ValueError, match="undocumented expected response"):
        validate_plan(edited)
    edited = plan.model_copy(deep=True)
    edited.spec.operations["health"].responses["200"].content["application/json"] = {"$ref": "#/x"}
    with pytest.raises(ValueError, match="resolved locally"):
        validate_plan(edited)
    edited = plan.model_copy(deep=True)
    edited.spec.operations["health"].path = "/health?x=1"
    with pytest.raises(ValueError, match="unsafe operation path"):
        validate_plan(edited)


def test_concurrency_is_bounded(plan: Plan) -> None:
    in_flight = peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return httpx.Response(500)

    subset = plan.model_copy(deep=True)
    subset.cases = [c for c in subset.cases if c.operation_id == "createWidget"]
    run = asyncio.run(
        execute(subset, "http://h.test", Config(concurrency=2), httpx.MockTransport(handler))
    )
    assert len(run.results) > 2 and peak == 2


def test_timeout_fails_the_case(plan: Plan) -> None:
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200)

    config = Config(timeout_seconds=0.05)
    run = asyncio.run(
        execute(health_plan(plan), "http://h.test", config, httpx.MockTransport(hang))
    )
    assert run.results[0].errors == ["request failed (TimeoutError)"]
