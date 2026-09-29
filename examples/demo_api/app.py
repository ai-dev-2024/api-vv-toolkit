"""Stateless demo with exactly two intentional deviations from its contract."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from api_vv.spec import parse_spec

SPEC = parse_spec(Path(__file__).with_name("openapi.yaml"))
CREATE_SCHEMA: Any = copy.deepcopy(SPEC.operations["createWidget"].request_schema)
# Seeded defect 1: the documented maximum is 8, but the implementation accepts 9.
CREATE_SCHEMA["properties"]["name"]["maxLength"] = 9


async def health(request: Request) -> Response:
    return JSONResponse({"status": "ok"}, headers={"X-Service-Version": "1.0"})


async def create_widget(request: Request) -> Response:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"error": "invalid JSON"}, status_code=422)
    if not Draft202012Validator(CREATE_SCHEMA).is_valid(body):
        return JSONResponse({"error": "invalid widget"}, status_code=422)
    return JSONResponse({"id": 1, "name": body["name"]}, status_code=201)


async def widget(request: Request) -> Response:
    try:
        widget_id = int(request.path_params["widget_id"])
    except ValueError:
        return JSONResponse({"error": "invalid id"}, status_code=422)
    if request.method == "DELETE":
        # Seeded defect 2: contract says 204, implementation sends 200.
        return Response(status_code=200)
    if widget_id == 999:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"id": widget_id, "name": "widget"})


async def secure(request: Request) -> Response:
    expected = os.environ.get("API_VV_DEMO_KEY", "demo-only-key")
    if request.headers.get("X-API-Key") != expected:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({"authorized": True})


app = Starlette(
    routes=[
        Route("/health", health),
        Route("/widgets", create_widget, methods=["POST"]),
        Route("/widgets/{widget_id}", widget, methods=["GET", "DELETE"]),
        Route("/secure", secure),
    ]
)
