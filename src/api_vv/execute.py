"""Bounded asynchronous execution and documented-response validation."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from typing import Any
from urllib.parse import quote

import httpx
from jsonschema import Draft202012Validator, FormatChecker

from api_vv.generate import case_problems, trace_case
from api_vv.io import check_base_url, plan_digest, unsafe_path
from api_vv.models import Case, Config, Operation, Plan, Result, Run, Schema, Spec
from api_vv.spec import is_json, validate_requirements


def validate_plan(plan: Plan) -> None:
    validate_requirements(plan.requirements, plan.spec)

    def no_refs(value: Any) -> None:
        if isinstance(value, dict):
            if any(key in value for key in ("$ref", "$dynamicRef", "$recursiveRef", "$id")):
                raise ValueError("plan schemas must be resolved locally before execution")
            for child in value.values():
                no_refs(child)
        elif isinstance(value, list):
            for child in value:
                no_refs(child)

    no_refs(plan.spec.model_dump(by_alias=True))
    # Plans are hand-edited, so re-check what the parser already enforced.
    for op in plan.spec.operations.values():
        if unsafe_path(op.path):
            raise ValueError(f"unsafe operation path: {op.path}")
    for case in plan.cases:
        if case.enabled:
            problems = case_problems(case, plan.spec)
            if problems:
                raise ValueError(f"{case.id}: {'; '.join(problems)}")
        traced = case.model_copy(deep=True)
        trace_case(traced, plan.requirements)
        if traced.requirement_ids != sorted(case.requirement_ids):
            raise ValueError(f"{case.id}: trace links disagree with requirement coverage selectors")


def response_key(status: int, op: Operation) -> str | None:
    return next(
        (key for key in (str(status), f"{status // 100}XX", "default") if key in op.responses), None
    )


def schema_errors(value: Any, schema: Schema, label: str) -> list[str]:
    return [
        f"{label} schema: /{'/'.join(map(str, error.absolute_path))} [{error.validator}]"
        for error in Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value)
    ]


def scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def header_value(value: str, schema: Schema) -> Any:
    kind = schema.get("type") if isinstance(schema, dict) else None
    try:
        if kind == "integer":
            return int(value)
        if kind == "number":
            return float(value)
        if kind == "boolean" and value.lower() in {"true", "false"}:
            return value.lower() == "true"
    except ValueError:
        pass
    return value


def validate_response(response: httpx.Response, case: Case, op: Operation) -> list[str]:
    errors: list[str] = []
    key = response_key(response.status_code, op)
    expected = case.expected_status
    status_matches = (
        str(response.status_code) == expected
        or (expected.endswith("XX") and str(response.status_code).startswith(expected[0]))
        or (expected == "default" and key == "default")
    )
    if not status_matches:
        errors.append(f"status: expected {expected}, got {response.status_code}")
    if key is None:
        return errors + ["response status is undocumented"]
    documented = op.responses[key]
    for name, header in documented.headers.items():
        value = response.headers.get(name)
        if value is None:
            if header.required:
                errors.append(f"missing response header: {name}")
        else:
            errors.extend(
                schema_errors(header_value(value, header.schema_), header.schema_, f"header {name}")
            )
    if documented.content:
        media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        schema = documented.content.get(media)
        if schema is None:
            schema = documented.content.get(
                media.split("/")[0] + "/*", documented.content.get("*/*")
            )
        if schema is None:
            errors.append("response content-type is missing or undocumented")
        elif is_json(media):
            try:
                errors.extend(schema_errors(response.json(), schema, "body"))
            except (ValueError, UnicodeDecodeError):
                errors.append("response body is not valid JSON")
        else:
            errors.extend(schema_errors(response.text, schema, "body"))
    elif response.content:
        errors.append("response body is undocumented")
    return errors


def request_parts(
    case: Case, op: Operation, spec: Spec, config: Config
) -> tuple[str, dict[str, str], dict[str, str], dict[str, str]]:
    path = op.path
    for name, value in case.parameters.get("path", {}).items():
        path = path.replace("{" + name + "}", quote(scalar(value), safe=""))
    if re.search(r"\{[^}]+\}", path):
        raise ValueError("unfilled path parameter")
    headers = {
        name.lower(): scalar(value) for name, value in case.parameters.get("header", {}).items()
    }
    query = {name: scalar(value) for name, value in case.parameters.get("query", {}).items()}
    cookies = {name: scalar(value) for name, value in case.parameters.get("cookie", {}).items()}
    locations = {"header": headers, "query": query, "cookie": cookies}
    if case.auth == "omit":
        for group in op.security:
            for name in group:
                scheme = spec.security_schemes[name]
                if scheme.type == "apiKey" and scheme.location and scheme.name:
                    key = scheme.name.lower() if scheme.location == "header" else scheme.name
                    locations[scheme.location].pop(key, None)
                else:
                    headers.pop("authorization", None)
        return path, headers, query, cookies
    if not op.security:
        return path, headers, query, cookies
    selected = next(
        (
            group
            for group in op.security
            if all(os.environ.get(config.credentials_env.get(name, ""), "") for name in group)
        ),
        None,
    )
    if selected is None:
        raise ValueError("missing configured credentials for operation security requirements")
    for name in selected:
        scheme = spec.security_schemes[name]
        secret = os.environ[config.credentials_env[name]]
        if scheme.type == "apiKey" and scheme.location and scheme.name:
            key = scheme.name.lower() if scheme.location == "header" else scheme.name
            locations[scheme.location][key] = secret
        elif scheme.type == "http" and scheme.scheme == "basic":
            headers["authorization"] = "Basic " + base64.b64encode(secret.encode()).decode()
        elif scheme.type in {"oauth2", "openIdConnect"} or scheme.scheme == "bearer":
            headers["authorization"] = "Bearer " + secret
        else:
            raise ValueError("unsupported authentication scheme")
    return path, headers, query, cookies


async def execute(
    plan: Plan,
    base_url: str,
    config: Config | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Run:
    validate_plan(plan)
    config = config or Config()
    check_base_url(base_url, "base URL")
    semaphore = asyncio.Semaphore(config.concurrency)
    requirements = {item.id: item for item in plan.requirements.requirements}
    async with httpx.AsyncClient(
        timeout=config.timeout_seconds, transport=transport, follow_redirects=False, trust_env=False
    ) as client:

        async def run_case(case: Case) -> Result:
            if not case.enabled:
                return Result(
                    case_id=case.id, verdict="skipped", errors=[case.review_reason or "disabled"]
                )
            op = plan.spec.operations[case.operation_id]
            try:
                path, headers, query, cookies = request_parts(case, op, plan.spec, config)
            except ValueError as exc:
                return Result(case_id=case.id, verdict="failed", errors=[str(exc)])
            if case.has_body:
                headers["content-type"] = op.request_media_type
            # Per-request Cookie headers avoid shared client cookie state between concurrent cases.
            if cookies:
                cookie_request = httpx.Request("GET", base_url, cookies=cookies)
                headers["cookie"] = cookie_request.headers["cookie"]
            attempts = 0
            async with semaphore:
                start = time.perf_counter()
                retry_limit = config.retries if op.method in {"GET", "HEAD", "OPTIONS"} else 0
                while True:
                    attempts += 1
                    try:
                        request = httpx.Request(
                            op.method,
                            base_url.rstrip("/") + path,
                            headers=headers,
                            params=query,
                            content=json.dumps(case.body, allow_nan=False).encode()
                            if case.has_body
                            else None,
                        )
                        # httpx timeouts are per phase; wait_for bounds the whole exchange.
                        response = await asyncio.wait_for(
                            client.send(request), timeout=config.timeout_seconds
                        )
                        break
                    except (httpx.HTTPError, TimeoutError, ValueError) as exc:
                        if attempts <= retry_limit and isinstance(
                            exc, (httpx.TransportError, TimeoutError)
                        ):
                            continue
                        return Result(
                            case_id=case.id,
                            verdict="failed",
                            errors=[f"request failed ({type(exc).__name__})"],
                            attempts=attempts,
                            latency_ms=(time.perf_counter() - start) * 1000,
                        )
                latency = (time.perf_counter() - start) * 1000
            errors = validate_response(response, case, op)
            for req_id in case.requirement_ids:
                budget = requirements[req_id].latency_ms
                if budget is not None and latency > budget:
                    errors.append(f"{req_id}: latency exceeds {budget:g} ms")
            return Result(
                case_id=case.id,
                verdict="failed" if errors else "passed",
                actual_status=response.status_code,
                latency_ms=latency,
                errors=errors,
                attempts=attempts,
            )

        results = await asyncio.gather(*(run_case(case) for case in plan.cases))
    return Run(plan_digest=plan_digest(plan), results=list(results))
