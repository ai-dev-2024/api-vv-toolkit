"""Traceability is computed from evidence, never from the presence of generated tests."""

from __future__ import annotations

import csv
import html
import io
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Literal

from jinja2 import Environment

from api_vv.execute import validate_plan
from api_vv.generate import matches
from api_vv.io import plan_digest
from api_vv.models import Case, Config, Model, Plan, Requirement, Result, Run

Status = Literal["verified", "failed", "not covered", "partially covered"]


class Row(Model):
    requirement: Requirement
    status: Status
    cases: list[Case]
    verdicts: dict[str, str]


class Matrix(Model):
    rows: list[Row]
    untraced_tests: list[str]
    operations_without_requirements: list[str]
    results: dict[str, Result]


def matrix(plan: Plan, run: Run | None = None) -> Matrix:
    validate_plan(plan)
    if run is not None and run.plan_digest != plan_digest(plan):
        raise ValueError("results belong to a different plan; execute the edited plan again")
    results = {result.case_id: result for result in run.results} if run else {}
    if results.keys() - {case.id for case in plan.cases}:
        raise ValueError("results contain unknown case ids")
    rows: list[Row] = []
    for requirement in plan.requirements.requirements:
        cases = [case for case in plan.cases if requirement.id in case.requirement_ids]
        verdicts = {
            case.id: results[case.id].verdict if case.id in results else "not run" for case in cases
        }
        passed = [case for case in cases if verdicts[case.id] == "passed"]
        covered = all(
            any(
                matches(case, cover.operation_id, [response], [check] if check else [])
                for case in passed
            )
            for cover in requirement.covers
            for response in cover.responses
            for check in (cover.checks or [""])
        )
        status: Status
        if "failed" in verdicts.values():
            status = "failed"
        elif not passed:
            status = "not covered"
        elif covered and all(value == "passed" for value in verdicts.values()):
            status = "verified"
        else:
            status = "partially covered"
        rows.append(Row(requirement=requirement, status=status, cases=cases, verdicts=verdicts))
    mapped_ops = {
        cover.operation_id
        for requirement in plan.requirements.requirements
        for cover in requirement.covers
    }
    return Matrix(
        rows=rows,
        untraced_tests=[case.id for case in plan.cases if not case.requirement_ids],
        operations_without_requirements=sorted(plan.spec.operations.keys() - mapped_ops),
        results=results,
    )


def gate_failures(data: Matrix, config: Config) -> list[str]:
    return [
        row.requirement.id
        for row in data.rows
        if row.requirement.priority in config.gate_priorities and row.status in config.gate_statuses
    ]


def escaped(value: str) -> str:
    return html.escape(value).replace("|", "&#124;").replace("\n", " ").replace("\r", " ")


def markdown(data: Matrix, *, matrix_only: bool = False) -> str:
    lines = [
        "# API V&V report",
        "",
        "| Requirement | Priority | Status | Test cases → latest verdict |",
        "| --- | --- | --- | --- |",
    ]
    for row in data.rows:
        tests = "; ".join(f"{case.id} → {row.verdicts[case.id]}" for case in row.cases) or "—"
        lines.append(
            f"| {row.requirement.id}: {escaped(row.requirement.text)} | "
            f"{row.requirement.priority} | {row.status} | {escaped(tests)} |"
        )
    lines += [
        "",
        "Untraced tests: " + (", ".join(data.untraced_tests) or "none"),
        "",
        "Operations without requirements: "
        + (", ".join(map(escaped, data.operations_without_requirements)) or "none"),
    ]
    if not matrix_only:
        lines += ["", "## Execution findings", ""]
        findings = [result for result in data.results.values() if result.errors]
        lines += [
            f"- {escaped(result.case_id)} ({result.verdict}): {escaped('; '.join(result.errors))}"
            for result in findings
        ] or ["No execution findings."]
    return "\n".join(lines) + "\n"


HTML_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>API V&amp;V report</title><style>
body{font:16px/1.5 system-ui,sans-serif;margin:2rem;color:#17212b;background:#fff}
table{border-collapse:collapse;width:100%}
th,td{border:1px solid #adb5bd;padding:.6rem;text-align:left}
th{background:#edf1f5}td{vertical-align:top}code{overflow-wrap:anywhere}
.failed{color:#a2191f}.verified{color:#12622b}li{margin:.4rem 0}
</style></head><body><h1>API V&amp;V report</h1>
<table><thead><tr><th>Requirement</th><th>Priority</th><th>Status</th>
<th>Tests → latest verdict</th></tr></thead>
<tbody>{% for row in data.rows %}<tr><td>{{ row.requirement.id }}: {{ row.requirement.text }}</td>
<td>{{ row.requirement.priority }}</td><td class="{{ row.status }}">{{ row.status }}</td><td>
{% for case in row.cases %}<div><code>{{ case.id }}</code> → {{ row.verdicts[case.id] }}</div>
{% else %}—{% endfor %}</td></tr>{% endfor %}</tbody></table>
<p>Untraced tests: {{ data.untraced_tests|join(', ') or 'none' }}</p>
<p>Operations without requirements:
{{ data.operations_without_requirements|join(', ') or 'none' }}</p>
{% if not matrix_only %}<h2>Execution findings</h2><ul>
{% for result in data.results.values() if result.errors %}<li><code>{{ result.case_id }}</code>
({{ result.verdict }}): {{ result.errors|join('; ') }}</li>
{% else %}<li>No execution findings.</li>{% endfor %}
</ul>{% endif %}</body></html>
"""


def html_report(data: Matrix, *, matrix_only: bool = False) -> str:
    return (
        Environment(autoescape=True)
        .from_string(HTML_TEMPLATE)
        .render(data=data, matrix_only=matrix_only)
    )


def csv_report(data: Matrix) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output)

    def safe(value: str) -> str:
        # Quoting alone does not stop spreadsheet formula execution.
        return "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value

    writer.writerow(
        [
            "kind",
            "requirement_id",
            "text",
            "priority",
            "status",
            "case_id",
            "operation_id",
            "expected_status",
            "verdict",
        ]
    )
    for row in data.rows:
        prefix = [
            "requirement",
            row.requirement.id,
            safe(row.requirement.text),
            row.requirement.priority,
            row.status,
        ]
        if not row.cases:
            writer.writerow(prefix + ["", "", "", "not run"])
        for case in row.cases:
            writer.writerow(
                prefix
                + [
                    safe(case.id),
                    safe(case.operation_id),
                    case.expected_status,
                    row.verdicts[case.id],
                ]
            )
    for case_id in data.untraced_tests:
        result = data.results.get(case_id)
        writer.writerow(
            [
                "untraced",
                "",
                "",
                "",
                "",
                safe(case_id),
                "",
                "",
                result.verdict if result else "not run",
            ]
        )
    for operation_id in data.operations_without_requirements:
        writer.writerow(["unmapped-operation", "", "", "", "", "", safe(operation_id), "", ""])
    return output.getvalue()


def junit(plan: Plan, data: Matrix, config: Config) -> str:
    suite = ET.Element("testsuite", name="api-vv", tests=str(len(plan.cases) + len(data.rows)))
    failures = 0
    skipped = 0
    for case in plan.cases:
        result = data.results.get(case.id)
        node = ET.SubElement(
            suite,
            "testcase",
            name=case.id,
            classname=case.operation_id,
            time=f"{(result.latency_ms or 0) / 1000:.6f}" if result else "0",
        )
        properties = ET.SubElement(node, "properties")
        ET.SubElement(
            properties, "property", name="requirements", value=",".join(case.requirement_ids)
        )
        if result is None or result.verdict == "skipped":
            ET.SubElement(node, "skipped", message=case.review_reason or "not run")
            skipped += 1
        elif result.verdict == "failed":
            ET.SubElement(node, "failure", message="; ".join(result.errors)).text = "\n".join(
                result.errors
            )
            failures += 1
    gated = set(gate_failures(data, config))
    for row in data.rows:
        node = ET.SubElement(suite, "testcase", name=row.requirement.id, classname="requirements")
        if row.requirement.id in gated:
            ET.SubElement(node, "failure", message=row.status)
            failures += 1
        elif row.status != "verified":
            ET.SubElement(node, "skipped", message=row.status)
            skipped += 1
    suite.set("failures", str(failures))
    suite.set("skipped", str(skipped))
    suite.set("errors", "0")
    ET.indent(suite)
    return ET.tostring(suite, encoding="unicode", xml_declaration=True) + "\n"


def write_reports(plan: Plan, run: Run, directory: str | Path, config: Config) -> Matrix:
    data = matrix(plan, run)
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    formats = {
        "report.md": markdown(data),
        "report.html": html_report(data),
        "traceability.csv": csv_report(data),
        "junit.xml": junit(plan, data, config),
    }
    for name, content in formats.items():
        (target / name).write_text(content, encoding="utf-8")
    return data
