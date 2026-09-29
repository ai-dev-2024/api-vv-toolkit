from __future__ import annotations

import csv
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx
import pytest

from api_vv import cli
from api_vv.execute import execute
from api_vv.io import read_data
from api_vv.models import Config, Plan, Run

DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo_api"
SPEC = str(DEMO / "openapi.yaml")
REQS = str(DEMO / "requirements.yaml")
CONFIG = str(DEMO / "config.yaml")


def call(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_end_to_end(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    transport: httpx.ASGITransport,
) -> None:
    monkeypatch.setenv("API_VV_DEMO_KEY", "cli-secret")

    async def in_process(plan: Plan, base_url: str, config: Config) -> Run:
        return await execute(plan, base_url, config, transport)

    monkeypatch.setattr(cli, "execute", in_process)
    plan_path, results = str(tmp_path / "plan.yaml"), str(tmp_path / "results.yaml")

    assert call(capsys, "parse", "--spec", SPEC, "--out", str(tmp_path / "spec.yaml")) == (
        0,
        "Parsed 5 operations\n",
        "",
    )
    assert call(capsys, "validate-requirements", "--spec", SPEC, "--requirements", REQS)[1] == (
        "Validated 12 requirements\n"
    )
    code, out, _ = call(
        capsys, "generate", "--spec", SPEC, "--requirements", REQS, "--config", CONFIG,
        "--llm", "--out", plan_path,
    )  # fmt: skip
    assert code == 0 and "LLM proposals: 1 accepted, 1 rejected" in out
    code, out, _ = call(
        capsys, "run", "--plan", plan_path, "--base-url", "http://demo.test",
        "--config", CONFIG, "--out", results,
    )  # fmt: skip
    assert code == 1
    assert out.endswith("2 failed, 0 skipped\nGate: FAIL (REQ-003, REQ-008)\n")
    assert "cli-secret" not in Path(results).read_text() + Path(plan_path).read_text()

    code, out, _ = call(
        capsys, "report", "--plan", plan_path, "--results", results, "--out-dir", str(tmp_path)
    )
    assert code == 1 and "Gate: FAIL (REQ-003, REQ-008)" in out
    suite = ET.parse(tmp_path / "junit.xml").getroot()
    failed = {
        node.get("name") for node in suite.iter("testcase") if node.find("failure") is not None
    }
    assert {"REQ-003", "REQ-008"} <= failed and len(failed) == 4
    with (tmp_path / "traceability.csv").open(newline="") as handle:
        rows = {row["requirement_id"]: row for row in csv.DictReader(handle)}
    assert rows["REQ-003"]["status"] == "failed" and rows["REQ-008"]["verdict"] == "failed"
    assert (tmp_path / "report.html").read_text().startswith("<!doctype html>")

    code, out, _ = call(capsys, "trace", "--plan", plan_path, "--results", results)
    assert code == 0 and "| must | failed |" in out
    trace_csv = tmp_path / "out" / "trace.csv"
    assert (
        call(capsys, "trace", "--plan", plan_path, "--format", "csv", "--out", str(trace_csv))[0]
        == 0
    )
    assert "not run" in trace_csv.read_text()
    code, out, _ = call(capsys, "trace", "--plan", plan_path, "--format", "html")
    assert code == 0 and "not covered" in out


def test_generate_without_llm(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "plan.yaml"
    code, text, _ = call(
        capsys, "generate", "--spec", SPEC, "--requirements", REQS, "--out", str(out)
    )
    assert code == 0 and "LLM" not in text
    assert Plan.model_validate(read_data(out)).llm_audit == []


@pytest.mark.parametrize(
    "content, message",
    [
        ("openapi: 2.0\n", "error: expected an OpenAPI 3.0.x or 3.1.x document"),
        (": [", "error: invalid or unreadable input (ParserError)"),
    ],
)
def test_invalid_input_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str, message: str
) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(content, encoding="utf-8")
    code, _, err = call(capsys, "parse", "--spec", str(bad), "--out", str(tmp_path / "o"))
    assert (code, err.strip()) == (2, message)


def test_validation_errors_do_not_echo_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "config.yaml"
    bad.write_text("credentials_env: sk-live-secret\n", encoding="utf-8")
    code, _, err = call(capsys, "trace", "--plan", str(bad), "--config", str(bad))
    assert code == 2 and "sk-live-secret" not in err
    assert err.startswith("error: ") and "credentials_env: extra_forbidden" in err


def test_missing_file_and_bad_schema(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = call(capsys, "parse", "--spec", str(tmp_path / "none.yaml"), "--out", "x")
    assert code == 2 and "FileNotFoundError" in err
    spec: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": "t"},
        "paths": {"/a": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }
    spec["paths"]["/a"]["get"]["parameters"] = [{"name": "q", "in": "query", "schema": {"type": 5}}]
    target = tmp_path / "spec.json"
    target.write_text(json.dumps(spec), encoding="utf-8")
    code, _, err = call(capsys, "parse", "--spec", str(target), "--out", str(tmp_path / "o"))
    assert (code, err) == (2, "error: invalid JSON schema\n")


def test_unknown_requirement_operation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    reqs = tmp_path / "reqs.yaml"
    reqs.write_text(
        "requirements:\n  - {id: REQ-001, text: t, priority: must, verification_method: test,"
        " covers: [{operation_id: nope, responses: ['200']}]}\n",
        encoding="utf-8",
    )
    code, _, err = call(
        capsys, "validate-requirements", "--spec", SPEC, "--requirements", str(reqs)
    )
    assert (code, err) == (2, "error: REQ-001: unknown operation nope\n")
