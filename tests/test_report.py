from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET
from collections.abc import Set

import pytest

from api_vv.generate import trace_case
from api_vv.io import plan_digest
from api_vv.models import Config, Coverage, Plan, Result, Run
from api_vv.report import csv_report, gate_failures, html_report, junit, markdown, matrix


def run_with(plan: Plan, failed: Set[str] = frozenset(), missing: Set[str] = frozenset()) -> Run:
    """Pass every enabled case except the ones named, which fail or have no result."""
    results = []
    for case in plan.cases:
        if case.id in missing:
            continue
        if not case.enabled:
            results.append(Result(case_id=case.id, verdict="skipped", errors=["review"]))
        elif case.id in failed:
            results.append(Result(case_id=case.id, verdict="failed", errors=["status: bad"]))
        else:
            results.append(Result(case_id=case.id, verdict="passed", latency_ms=1.5))
    return Run(plan_digest=plan_digest(plan), results=results)


def ids_for(plan: Plan, req_id: str) -> set[str]:
    return {case.id for case in plan.cases if req_id in case.requirement_ids}


def statuses(plan: Plan, run: Run | None) -> dict[str, str]:
    return {row.requirement.id: row.status for row in matrix(plan, run).rows}


def test_status_logic(plan: Plan) -> None:
    assert set(statuses(plan, None).values()) == {"not covered"}
    assert set(statuses(plan, run_with(plan)).values()) == {"verified"}
    failing = statuses(plan, run_with(plan, failed=ids_for(plan, "REQ-008")))
    assert failing["REQ-008"] == "failed" and failing["REQ-001"] == "verified"
    missing = statuses(plan, run_with(plan, missing=ids_for(plan, "REQ-009")))
    assert missing["REQ-009"] == "not covered"


def retrace(plan: Plan) -> None:
    for case in plan.cases:
        trace_case(case, plan.requirements)


def test_partial_coverage(plan: Plan) -> None:
    edited = plan.model_copy(deep=True)
    covers = edited.requirements.requirements[1].covers
    covers.append(
        Coverage(operation_id="createWidget", responses=["422"], checks=["required:name"])
    )
    retrace(edited)
    assert statuses(edited, run_with(edited))["REQ-002"] == "verified"
    # A selector that no passing test satisfies leaves the evidence incomplete.
    covers.append(Coverage(operation_id="createWidget", responses=["422"], checks=["unmatched"]))
    assert statuses(edited, run_with(edited))["REQ-002"] == "partially covered"


def test_matrix_rejects_foreign_results(plan: Plan) -> None:
    run = run_with(plan)
    with pytest.raises(ValueError, match="different plan"):
        matrix(plan, run.model_copy(update={"plan_digest": "0" * 64}))
    run.results.append(Result(case_id="TC-unknown", verdict="passed"))
    with pytest.raises(ValueError, match="unknown case ids"):
        matrix(plan, run)


def test_gate_is_configurable(plan: Plan) -> None:
    # REQ-001 (must) and REQ-012 (should) share the health happy-path case.
    data = matrix(plan, run_with(plan, failed=ids_for(plan, "REQ-012")))
    assert gate_failures(data, Config()) == ["REQ-001"]
    both = Config(gate_priorities=["must", "should"])
    assert gate_failures(data, both) == ["REQ-001", "REQ-012"]
    assert gate_failures(data, Config(gate_statuses=["not covered"])) == []


def test_markdown_and_html(plan: Plan) -> None:
    plan = plan.model_copy(deep=True)
    plan.requirements.requirements[0].text = "Pipes | and <b>tags</b>"
    run = run_with(plan, failed=ids_for(plan, "REQ-008"))
    data = matrix(plan, run)
    text = markdown(data)
    assert "| REQ-008: Deleting a widget returns 204 with no body. | must | failed |" in text
    assert "&#124; and &lt;b&gt;" in text
    assert "## Execution findings" in text and "status: bad" in text
    assert "Execution findings" not in markdown(data, matrix_only=True)
    page = html_report(data)
    assert "<b>tags" not in page and "&lt;b&gt;tags" in page
    assert "<script" not in page and "http" not in page.split("<body>")[1]
    assert '<td class="failed">failed</td>' in page
    assert "Execution findings" not in html_report(data, matrix_only=True)
    clean = matrix(plan, run_with(plan))
    assert "No execution findings." in markdown(clean)
    assert "No execution findings." in html_report(clean)


def test_csv_round_trip_and_formula_guard(plan: Plan) -> None:
    plan = plan.model_copy(deep=True)
    plan.requirements.requirements[0].text = "=HYPERLINK(1)"
    plan.requirements.requirements[11].covers[0].checks = ["unmatched-check"]
    retrace(plan)
    rows = list(csv.DictReader(io.StringIO(csv_report(matrix(plan, run_with(plan))))))
    first = rows[0]
    assert first["kind"] == "requirement" and first["text"] == "'=HYPERLINK(1)"
    assert first["status"] == "verified" and first["verdict"] == "passed"
    kinds = {row["kind"] for row in rows}
    assert kinds == {"requirement", "untraced"}
    empty = next(row for row in rows if row["requirement_id"] == "REQ-012")
    assert empty["case_id"] == "" and empty["verdict"] == "not run"
    untraced = {row["case_id"] for row in rows if row["kind"] == "untraced"}
    assert untraced == {case.id for case in plan.cases if not case.requirement_ids}


def test_csv_lists_unmapped_operations(plan: Plan) -> None:
    plan = plan.model_copy(deep=True)
    plan.requirements.requirements = [
        req for req in plan.requirements.requirements if req.id != "REQ-008"
    ]
    for case in plan.cases:
        case.requirement_ids = [r for r in case.requirement_ids if r != "REQ-008"]
    data = matrix(plan, None)
    assert data.operations_without_requirements == ["deleteWidget"]
    rows = list(csv.reader(io.StringIO(csv_report(data))))
    assert ["unmapped-operation", "", "", "", "", "", "deleteWidget", "", ""] in rows
    assert "Operations without requirements: deleteWidget" in markdown(data)


def test_junit_parses_back(plan: Plan) -> None:
    plan = plan.model_copy(deep=True)
    plan.cases[0].enabled = False
    plan.cases[0].review_reason = "needs review"
    failed = ids_for(plan, "REQ-003")
    run = run_with(plan, failed=failed, missing={plan.cases[1].id})
    root = ET.fromstring(junit(plan, matrix(plan, run), Config()))
    cases = root.findall("testcase")
    assert int(root.get("tests", "0")) == len(cases) == len(plan.cases) + 12
    by_name = {node.get("name"): node for node in cases}
    (case_id,) = failed
    failure = by_name[case_id].find("failure")
    assert failure is not None and failure.get("message") == "status: bad"
    requirement = by_name["REQ-003"].find("failure")
    assert requirement is not None and requirement.get("message") == "failed"
    skipped = by_name[plan.cases[0].id].find("skipped")
    assert skipped is not None and skipped.get("message") == "needs review"
    prop = by_name[case_id].find("properties/property")
    assert prop is not None and prop.get("value") == "REQ-003"
    assert root.get("failures") == str(len(root.findall("testcase/failure")))
    assert root.get("skipped") == str(len(root.findall("testcase/skipped")))
