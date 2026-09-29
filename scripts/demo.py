"""Run the real CLI against a managed loopback server; assert the expected gate."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from api_vv.io import read_data
from api_vv.models import Plan, Run
from api_vv.report import matrix

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "runs" / "demo"
EXAMPLE = "examples/demo_api/"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, API_VV_DEMO_KEY="demo-only-key")
    # Pass the bound socket to the child: no free-port discovery race.
    with socket.socket() as listener, (OUT / "server.log").open("w") as log:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "examples.demo_api.app:app",
                "--fd",
                str(listener.fileno()),
                "--no-access-log",
            ],
            cwd=ROOT,
            env=env,
            pass_fds=(listener.fileno(),),
            stdout=log,
            stderr=log,
        )
        try:
            url = f"http://127.0.0.1:{port}"
            with httpx.Client(trust_env=False, timeout=0.5) as client:
                for _ in range(100):
                    if server.poll() is not None:
                        raise RuntimeError("demo server exited; see runs/demo/server.log")
                    try:
                        if client.get(url + "/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.05)
                else:
                    raise RuntimeError("demo server did not become ready")
            commands: list[tuple[list[str], int]] = [
                (
                    [
                        "validate-requirements",
                        "--spec",
                        EXAMPLE + "openapi.yaml",
                        "--requirements",
                        EXAMPLE + "requirements.yaml",
                    ],
                    0,
                ),
                (
                    [
                        "generate",
                        "--spec",
                        EXAMPLE + "openapi.yaml",
                        "--requirements",
                        EXAMPLE + "requirements.yaml",
                        "--config",
                        EXAMPLE + "config.yaml",
                        "--llm",
                        "--out",
                        "runs/demo/plan.yaml",
                    ],
                    0,
                ),
                (
                    [
                        "run",
                        "--plan",
                        "runs/demo/plan.yaml",
                        "--base-url",
                        url,
                        "--config",
                        EXAMPLE + "config.yaml",
                        "--out",
                        "runs/demo/results.yaml",
                    ],
                    1,
                ),
                (
                    [
                        "report",
                        "--plan",
                        "runs/demo/plan.yaml",
                        "--results",
                        "runs/demo/results.yaml",
                        "--out-dir",
                        "runs/demo",
                    ],
                    1,
                ),
            ]
            captured: list[str] = []
            for args, expected in commands:
                result = subprocess.run(
                    [sys.executable, "-m", "api_vv.cli", *args],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                print(result.stdout, end="", flush=True)
                captured.append(result.stdout)
                if result.returncode != expected:
                    raise RuntimeError(f"unexpected demo exit {result.returncode}: {result.stderr}")
            plan = Plan.model_validate(read_data(OUT / "plan.yaml"))
            run = Run.model_validate(read_data(OUT / "results.yaml"))
            data = matrix(plan, run)
            failing = {row.requirement.id for row in data.rows if row.status != "verified"}
            assert failing == {"REQ-003", "REQ-008"}, failing
            failures = [result for result in run.results if result.verdict == "failed"]
            assert len(failures) == 2, failures
            message = "Expected defects confirmed: REQ-003, REQ-008\n"
            print(message, end="")
            captured.append(message)
            (OUT / "cli-output.txt").write_text("".join(captured), encoding="utf-8")
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
