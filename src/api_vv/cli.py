"""Small argparse interface; exit 1 means a failed gate, exit 2 invalid input."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import yaml
from jsonschema.exceptions import SchemaError
from pydantic import ValidationError

from api_vv import __version__
from api_vv.execute import execute
from api_vv.generate import generate
from api_vv.io import read_data, write_data
from api_vv.llm import augment
from api_vv.models import Config, Plan, Requirements, Run
from api_vv.report import (
    Matrix,
    csv_report,
    gate_failures,
    html_report,
    markdown,
    matrix,
    write_reports,
)
from api_vv.spec import parse_spec, validate_requirements


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="api-vv", description=__doc__)
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("parse", "validate-requirements", "generate"):
        sub = commands.add_parser(name)
        sub.add_argument("--spec", required=True)
        if name != "parse":
            sub.add_argument("--requirements", required=True)
        if name != "validate-requirements":
            sub.add_argument("--out", required=True)
        if name == "generate":
            sub.add_argument(
                "--llm", action="store_true", help="enable configured provider (default: mock)"
            )
            sub.add_argument("--config")
    for name in ("run", "report", "trace"):
        sub = commands.add_parser(name)
        sub.add_argument("--plan", required=True)
        sub.add_argument("--config")
        if name == "run":
            sub.add_argument("--base-url", required=True)
            sub.add_argument("--out", required=True)
        else:
            sub.add_argument("--results", required=name == "report")
            if name == "report":
                sub.add_argument("--out-dir", required=True)
            else:
                sub.add_argument("--format", choices=["md", "html", "csv"], default="md")
                sub.add_argument("--out")
    return root


def load_config(path: str | None) -> Config:
    if path is None:
        return Config()
    config = Config.model_validate(read_data(path))
    if config.llm.fixtures:
        config.llm.fixtures = str(Path(path).resolve().parent / config.llm.fixtures)
    return config


def announce_gate(data: Matrix, config: Config) -> int:
    failed = gate_failures(data, config)
    print("Gate: FAIL (" + ", ".join(failed) + ")" if failed else "Gate: PASS")
    return 1 if failed else 0


def dispatch(args: argparse.Namespace) -> int:
    if args.command in {"parse", "validate-requirements", "generate"}:
        spec = parse_spec(args.spec)
        if args.command == "parse":
            write_data(args.out, spec)
            print(f"Parsed {len(spec.operations)} operations")
            return 0
        requirements = Requirements.model_validate(read_data(args.requirements))
        validate_requirements(requirements, spec)
        if args.command == "validate-requirements":
            print(f"Validated {len(requirements.requirements)} requirements")
            return 0
        plan = generate(spec, requirements)
        if args.llm:
            plan = asyncio.run(augment(plan, load_config(args.config).llm))
        write_data(args.out, plan)
        enabled = sum(case.enabled for case in plan.cases)
        print(f"Generated {len(plan.cases)} cases ({enabled} enabled)")
        if args.llm:
            accepted = sum(review.accepted for audit in plan.llm_audit for review in audit.reviews)
            rejected = sum(
                not review.accepted for audit in plan.llm_audit for review in audit.reviews
            )
            print(f"LLM proposals: {accepted} accepted, {rejected} rejected")
        return 0
    plan = Plan.model_validate(read_data(args.plan))
    config = load_config(args.config)
    if args.command == "run":
        run = asyncio.run(execute(plan, args.base_url, config))
        write_data(args.out, run)
        counts = {
            verdict: sum(item.verdict == verdict for item in run.results)
            for verdict in ("passed", "failed", "skipped")
        }
        print("Executed: " + ", ".join(f"{count} {verdict}" for verdict, count in counts.items()))
        return announce_gate(matrix(plan, run), config)
    if args.command == "report":
        data = write_reports(
            plan, Run.model_validate(read_data(args.results)), args.out_dir, config
        )
        print(f"Wrote Markdown, HTML, CSV and JUnit reports to {args.out_dir}")
        return announce_gate(data, config)
    trace_run = Run.model_validate(read_data(args.results)) if args.results else None
    data = matrix(plan, trace_run)
    content = (
        csv_report(data)
        if args.format == "csv"
        else html_report(data, matrix_only=True)
        if args.format == "html"
        else markdown(data, matrix_only=True)
    )
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    else:
        print(content, end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return dispatch(args)
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError, SchemaError) as exc:
        # ValidationError input values may contain authored credentials; show only locations.
        if isinstance(exc, ValidationError):
            detail = "; ".join(
                ".".join(map(str, error["loc"])) + ": " + error["type"] for error in exc.errors()
            )
        elif isinstance(exc, (ValueError, SchemaError)):
            detail = "invalid JSON schema" if isinstance(exc, SchemaError) else str(exc)
        else:
            detail = f"invalid or unreadable input ({type(exc).__name__})"
        print(f"error: {detail}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
