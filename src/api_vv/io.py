"""Safe serialization and fingerprints (never include runtime credentials)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import BaseModel

from api_vv.models import Case, Plan


def read_data(path: str | Path) -> Any:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def write_data(path: str | Path, model: BaseModel) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(model.model_dump(mode="json", by_alias=True), sort_keys=False),
        encoding="utf-8",
    )


def check_base_url(value: str, label: str) -> None:
    """Credentials are only ever sent to a plain HTTP(S) origin the user named."""
    url = httpx.URL(value)
    if (
        url.scheme not in {"http", "https"}
        or not url.host
        or url.userinfo
        or url.query
        or url.fragment
    ):
        raise ValueError(f"{label} must be an HTTP(S) URL without credentials, query or fragment")


def unsafe_path(path: str) -> bool:
    return (
        not path.startswith("/")
        or path.startswith("//")
        or "?" in path
        or "#" in path
        or any(part in {".", ".."} for part in path.split("/"))
    )


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def plan_digest(plan: Plan) -> str:
    return digest(plan.model_dump(mode="json", by_alias=True))


def fingerprint(case: Case) -> str:
    return digest(
        {
            "operation": case.operation_id,
            "parameters": case.parameters,
            "body": case.body,
            "has_body": case.has_body,
            "auth": case.auth,
        }
    )
