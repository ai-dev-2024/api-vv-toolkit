"""OpenAPI subset parser; unsupported constructs fail explicitly."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote

from jsonschema import Draft202012Validator

from api_vv.io import read_data, unsafe_path
from api_vv.models import (
    Header,
    Operation,
    Parameter,
    Requirements,
    Response,
    Schema,
    SecurityScheme,
    Spec,
)


def resolve_refs(document: dict[str, Any]) -> dict[str, Any]:
    """Resolve JSON Pointers within this document, without any network access."""

    def visit(value: Any, stack: tuple[str, ...] = ()) -> Any:
        if isinstance(value, list):
            return [visit(item, stack) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" not in value:
            return {key: visit(item, stack) for key, item in value.items()}
        ref = value["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise ValueError(f"only local document $refs are supported: {ref!r}")
        if ref in stack:
            raise ValueError(f"recursive $ref is unsupported: {ref}")
        target: Any = document
        try:
            for part in unquote(ref[2:]).split("/"):
                key = part.replace("~1", "/").replace("~0", "~")
                target = target[int(key)] if isinstance(target, list) else target[key]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError(f"unresolved local $ref: {ref}") from exc
        resolved = visit(target, (*stack, ref))
        siblings = {key: visit(item, stack) for key, item in value.items() if key != "$ref"}
        if siblings:
            if not isinstance(resolved, dict):
                raise ValueError("$ref siblings require an object target")
            # Schema siblings are conjunctive in 3.1; do not overwrite constraints.
            if document.get("openapi", "").startswith("3.1"):
                return {"allOf": [resolved, siblings]}
        return resolved

    return cast(dict[str, Any], visit(document))


def normalize_schema(schema: Schema, *, legacy: bool) -> Schema:
    if isinstance(schema, bool):
        return schema
    result = copy.deepcopy(schema)
    for key in ("$id", "$schema", "$dynamicRef", "$recursiveRef"):
        if key in result:
            raise ValueError(f"unsupported schema keyword: {key}")
    if legacy:
        if result.pop("nullable", False):
            original_type = result.get("type")
            if isinstance(original_type, str):
                result["type"] = [original_type, "null"]
            if "enum" in result and None not in result["enum"]:
                result["enum"].append(None)
        for bound in ("minimum", "maximum"):
            exclusive = "exclusive" + bound.title()
            flag = result.get(exclusive)
            if isinstance(flag, bool):
                result.pop(exclusive)
                if flag and bound in result:
                    result[exclusive] = result.pop(bound)
    for key in ("properties", "patternProperties", "$defs", "dependentSchemas"):
        if key in result:
            result[key] = {
                name: normalize_schema(item, legacy=legacy) for name, item in result[key].items()
            }
    for key in ("items", "additionalProperties", "not", "contains", "if", "then", "else"):
        if key in result:
            result[key] = normalize_schema(result[key], legacy=legacy)
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        if key in result:
            result[key] = [normalize_schema(item, legacy=legacy) for item in result[key]]
    Draft202012Validator.check_schema(result)
    return result


def schema_for(schema: Schema, direction: str) -> Schema:
    """Apply OpenAPI readOnly/writeOnly required-field semantics recursively."""
    if isinstance(schema, bool):
        return schema
    result = copy.deepcopy(schema)
    properties = result.get("properties", {})
    excluded = {
        name
        for name, prop in properties.items()
        if isinstance(prop, dict)
        and prop.get("readOnly" if direction == "request" else "writeOnly")
    }
    if "required" in result:
        result["required"] = [name for name in result["required"] if name not in excluded]
    if properties:
        result["properties"] = {
            name: schema_for(prop, direction) for name, prop in properties.items()
        }
    if "items" in result:
        result["items"] = schema_for(result["items"], direction)
    for key in ("allOf", "anyOf", "oneOf"):
        if key in result:
            result[key] = [schema_for(item, direction) for item in result[key]]
    return result


def is_json(media_type: str) -> bool:
    media = media_type.split(";", 1)[0].strip().lower()
    return media == "application/json" or media.endswith("+json")


def parse_spec(path: str | Path) -> Spec:
    return parse_document(read_data(path))


def parse_document(raw: Any) -> Spec:
    if not isinstance(raw, dict) or not re.fullmatch(r"3\.[01]\.\d+", str(raw.get("openapi"))):
        raise ValueError("expected an OpenAPI 3.0.x or 3.1.x document")
    doc = resolve_refs(raw)
    legacy = str(doc["openapi"]).startswith("3.0")

    def schema(value: Any, direction: str) -> Schema:
        if not isinstance(value, (dict, bool)):
            raise ValueError("schema must be an object or boolean")
        return schema_for(normalize_schema(value, legacy=legacy), direction)

    schemes = {
        name: SecurityScheme(
            type=item["type"],
            name=item.get("name"),
            location=item.get("in"),
            scheme=item.get("scheme"),
        )
        for name, item in doc.get("components", {}).get("securitySchemes", {}).items()
    }
    operations: dict[str, Operation] = {}
    for path, item in doc.get("paths", {}).items():
        if unsafe_path(path):
            raise ValueError(f"unsafe operation path: {path}")
        for method in ("get", "post", "put", "patch", "delete", "head", "options", "trace"):
            if method not in item:
                continue
            value = item[method]
            op_id = value.get("operationId", f"{method.upper()} {path}")
            if op_id in operations:
                raise ValueError(f"duplicate operationId: {op_id}")
            params: dict[tuple[str, str], Parameter] = {}
            for param in [*item.get("parameters", []), *value.get("parameters", [])]:
                location = param["in"]
                default_style = "form" if location in {"query", "cookie"} else "simple"
                if param.get("style", default_style) != default_style:
                    raise ValueError("unsupported parameter style; use form or simple")
                param_schema = schema(param.get("schema", {}), "request")
                if isinstance(param_schema, dict) and param_schema.get("type") in {
                    "object",
                    "array",
                }:
                    raise ValueError("only scalar parameters are supported in v1")
                parsed = Parameter(
                    name=param["name"],
                    location=location,
                    required=location == "path" or param.get("required", False),
                    schema=param_schema,
                    example=param.get("example"),
                )
                params[(location, parsed.name)] = parsed
            placeholders = set(re.findall(r"\{([^}]+)\}", path))
            path_names = {name for location, name in params if location == "path"}
            if placeholders != path_names:
                raise ValueError(f"path parameters do not match placeholders: {op_id}")
            body = value.get("requestBody", {})
            content = body.get("content", {})
            media = next((key for key in content if is_json(key)), "application/json")
            if content and media not in content:
                raise ValueError(f"only JSON request bodies are supported: {op_id}")
            body_schema = None
            if content:
                body_schema = schema(content[media].get("schema", {}), "request")
                if isinstance(body_schema, dict) and "example" in content[media]:
                    body_schema["example"] = content[media]["example"]
            responses: dict[str, Response] = {}
            for code, response in value.get("responses", {}).items():
                code = str(code)
                if not re.fullmatch(r"(?:[1-5][0-9]{2}|[1-5]XX|default)", code):
                    raise ValueError(f"invalid response code: {code}")
                responses[code] = Response(
                    content={
                        key: schema(entry.get("schema", {}), "response")
                        for key, entry in response.get("content", {}).items()
                    },
                    headers={
                        name: Header(
                            required=header.get("required", False),
                            schema=schema(header.get("schema", {}), "response"),
                        )
                        for name, header in response.get("headers", {}).items()
                    },
                )
            if not responses:
                raise ValueError(f"operation has no responses: {op_id}")
            security = value.get("security", doc.get("security", []))
            if any(name not in schemes for group in security for name in group):
                raise ValueError(f"unknown security scheme: {op_id}")
            operations[op_id] = Operation(
                id=op_id,
                method=method.upper(),
                path=path,
                parameters=list(params.values()),
                request_schema=body_schema,
                request_required=body.get("required", False),
                request_media_type=media,
                responses=responses,
                security=security,
            )
    if not operations:
        raise ValueError("spec has no operations")
    return Spec(
        openapi=doc["openapi"],
        title=doc.get("info", {}).get("title", "API"),
        operations=operations,
        security_schemes=schemes,
    )


def validate_requirements(requirements: Requirements, spec: Spec) -> None:
    for requirement in requirements.requirements:
        for cover in requirement.covers:
            if cover.operation_id not in spec.operations:
                raise ValueError(f"{requirement.id}: unknown operation {cover.operation_id}")
            unknown = set(cover.responses) - spec.operations[cover.operation_id].responses.keys()
            if unknown:
                raise ValueError(f"{requirement.id}: undocumented responses {sorted(unknown)}")
