# Contributing

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,demo]'
```

## Before opening a pull request

```bash
make check   # ruff check, ruff format --check, mypy --strict src, pytest (coverage >= 85%)
make demo    # must still report exactly the two seeded defects
```

## Ground rules

- Tests never touch the network. `tests/conftest.py` blocks sockets; use `httpx.MockTransport`
  or `httpx.ASGITransport` instead.
- LLM behaviour is tested only through the `mock` provider or a mocked transport. Do not add
  tests that need an API key.
- A change to generator output changes case ids. Regenerate `docs/sample-report.md` and
  `docs/sample-traceability.csv` from `make demo` in the same pull request.
- Do not weaken the validation gate for model proposals to make a fixture pass; fix the fixture.
- Keep runtime dependencies to the current five (pydantic, PyYAML, httpx, jsonschema, jinja2).
- Commit messages follow the Conventional Commits style (`feat:`, `fix:`, `test:`, `docs:`, `ci:`).
