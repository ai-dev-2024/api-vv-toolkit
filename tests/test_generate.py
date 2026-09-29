from __future__ import annotations

from typing import Any

import pytest

from api_vv.generate import (
    case_problems,
    deduplicate,
    generate,
    mutations,
    operation_cases,
    sample,
    trace_case,
    valid,
)
from api_vv.models import Case, Requirements, Spec
from api_vv.spec import parse_document


def case(**overrides: Any) -> Case:
    values: dict[str, Any] = {
        "id": "pending",
        "name": "n",
        "operation_id": "health",
        "expected_status": "200",
        "generators": ["test"],
        "checks": ["happy-path"],
    }
    values.update(overrides)
    return Case(**values)


def by_check(cases: list[Case], check: str) -> Case:
    return next(item for item in cases if check in item.checks)


@pytest.mark.parametrize(
    "schema, expected",
    [
        (True, None),
        ({"type": "string", "example": "e", "default": "d"}, "e"),
        ({"type": "string", "default": "d"}, "d"),
        ({"const": 3}, 3),
        ({"examples": ["a", "b"]}, "a"),
        ({"enum": ["x", "y"]}, "x"),
        ({"oneOf": [{"type": "boolean"}]}, True),
        ({"anyOf": [{"type": "null"}]}, None),
        (
            {"allOf": [{"properties": {"a": {"const": 1}}}, {"properties": {"b": {"const": 2}}}]},
            {"a": 1, "b": 2},
        ),
        ({"allOf": [{"type": "integer"}, {"minimum": 5}]}, 0),
        ({"type": ["null", "integer"], "minimum": 3}, 3),
        ({"type": "integer", "exclusiveMinimum": 3, "maximum": 10}, 4),
        ({"type": "integer", "minimum": 7, "multipleOf": 5}, 10),
        ({"type": "number", "exclusiveMaximum": -2}, -3),
        ({"type": "array", "items": {"type": "boolean"}, "minItems": 2}, [True, True]),
        ({"properties": {"id": {"readOnly": True}, "n": {"type": "integer"}}}, {"n": 0}),
        ({"type": "string", "format": "uuid"}, "00000000-0000-4000-8000-000000000001"),
        ({"type": "string", "minLength": 3, "maxLength": 5}, "xxx"),
        ({"type": "string", "maxLength": 0}, ""),
    ],
)
def test_sample(schema: Any, expected: Any) -> None:
    assert sample(schema) == expected


@pytest.mark.parametrize(
    "schema", [{"type": "array", "minItems": 1001}, {"type": "string", "minLength": 10001}]
)
def test_sample_limits(schema: Any) -> None:
    with pytest.raises(ValueError, match="synthesis limit"):
        sample(schema)


def test_mutations_cover_every_rule() -> None:
    schema = {
        "type": "object",
        "required": ["name", "tags"],
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string", "minLength": 0, "maxLength": 2},
            "kind": {"enum": ["__outside_enum__"]},
            "n": {"type": "number", "minimum": 0, "multipleOf": 0.5},
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 1},
            "__unexpected__": {"type": "boolean"},
        },
    }
    value = sample(schema)
    found = {check: mutated for mutated, check in mutations(value, schema, "")}
    assert found["boundary:name:minLength:at"]["name"] == ""
    assert "boundary:name:minLength:below" not in found
    assert found["boundary:name:maxLength:above"]["name"] == "xxx"
    assert found["enum:kind"]["kind"] == "__outside_enum___"
    assert found["boundary:n:minimum:below"]["n"] == -0.5
    assert found["boundary:tags:maxItems:above"]["tags"] == ["x", "x"]
    assert "name" not in found["required:name"]
    assert found["wrong-type:name"]["name"] == 1
    assert found["wrong-type:"] == "wrong"
    assert "__unexpected___" in found["additional-properties:body"]
    assert list(mutations(None, True, "x")) == []
    assert list(mutations("x", {"maxLength": 10001}, "big")) == []


def test_demo_plan_is_traced(spec: Spec, requirements: Requirements) -> None:
    plan = generate(spec, requirements)
    ids = [item.id for item in plan.cases]
    assert len(ids) == len(set(ids)) and all(i.startswith("TC-") for i in ids)
    traced = {req: [c for c in plan.cases if req in c.requirement_ids] for req in ("REQ-003",)}
    (above,) = traced["REQ-003"]
    assert above.body["name"] == "x" * 9 and above.negative and above.expected_status == "422"
    auth = by_check(plan.cases, "auth-required")
    assert auth.auth == "omit" and auth.expected_status == "401"
    # Generation is deterministic, so ids are stable across runs.
    assert [c.id for c in generate(spec, requirements).cases] == ids


def test_requirement_selectors(spec: Spec, requirements: Requirements) -> None:
    item = case(checks=["happy-path"])
    trace_case(item, requirements)
    assert item.requirement_ids == ["REQ-001", "REQ-012"]
    inspected = requirements.model_copy(deep=True)
    inspected.requirements[0].verification_method = "inspection"
    trace_case(item, inspected)
    assert item.requirement_ids == ["REQ-012"]


def test_case_problems(spec: Spec) -> None:
    assert case_problems(case(operation_id="nope"), spec) == ["unknown operation"]
    problems = case_problems(
        case(
            operation_id="getWidget",
            expected_status="500",
            parameters={"path": {"widget_id": "."}, "query": {"x": [1]}},
            has_body=True,
            body={},
        ),
        spec,
    )
    assert problems == [
        "undocumented expected response",
        "invalid parameter: path.widget_id",
        "unsafe path parameter: widget_id",
        "unknown parameter: query.x",
        "non-scalar parameter unsupported: query.x",
        "operation has no request body",
    ]
    assert case_problems(case(operation_id="getWidget"), spec) == [
        "missing parameter: path.widget_id"
    ]
    create = case(operation_id="createWidget", expected_status="201")
    assert case_problems(create, spec) == ["required body is missing"]
    create.has_body, create.body = True, {"name": 1}
    assert case_problems(create, spec) == ["invalid body requires negative=true"]
    create.negative = True
    assert case_problems(create, spec) == []


def test_deduplicate_merges_and_rejects_conflicts() -> None:
    first = case(generators=["a"], checks=["one"], requirement_ids=["REQ-001"])
    second = case(generators=["b"], checks=["two"], requirement_ids=["REQ-002"])
    (merged,) = deduplicate([first, second])
    assert merged.generators == ["a", "b"] and merged.checks == ["one", "two"]
    assert merged.requirement_ids == ["REQ-001", "REQ-002"]
    review = case(enabled=False, expected_status="500")
    assert len(deduplicate([case(), review])) == 2
    assert deduplicate([review.model_copy()])[0].id.endswith("-review-500")
    with pytest.raises(ValueError, match="conflicting"):
        deduplicate([case(), case(expected_status="201")])


def small_spec(operation: dict[str, Any], **components: Any) -> Spec:
    return parse_document(
        {
            "openapi": "3.0.3",
            "info": {"title": "t"},
            "paths": {"/x": {"post": {"operationId": "op", **operation}}},
            "components": components,
        }
    )


def test_review_cases_when_expectations_are_undocumented() -> None:
    spec = small_spec(
        {
            "security": [{"key": []}],
            "parameters": [{"name": "q", "in": "query", "required": True, "schema": {"enum": [1]}}],
            "requestBody": {"content": {"application/json": {"schema": {"type": "integer"}}}},
            "responses": {"200": {"description": "ok"}, "500": {"description": "err"}},
        },
        securitySchemes={"key": {"type": "apiKey", "in": "header", "name": "K"}},
    )
    cases = operation_cases(spec.operations["op"], spec)
    disabled = {c.checks[0]: c.review_reason for c in cases if not c.enabled}
    assert disabled["enum:query.q"] == "no documented validation-error response; review expectation"
    assert disabled["auth-required"] == "no documented authentication-error response"
    assert str(disabled["documented-status"]).startswith("supply a request")
    assert by_check(cases, "happy-path").enabled


def test_unsatisfiable_happy_path_needs_review() -> None:
    spec = small_spec(
        {
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": {"type": "string", "example": 5}}},
            },
            "responses": {"400": {"description": "bad"}},
        }
    )
    base, status = operation_cases(spec.operations["op"], spec)
    assert not base.enabled
    assert base.review_reason == "invalid body requires negative=true"
    assert not status.enabled and status.expected_status == "400" and status.negative
    assert valid(5, {"type": "integer"})
