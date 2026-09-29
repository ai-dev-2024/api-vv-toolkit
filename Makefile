PYTHON ?= .venv/bin/python

.PHONY: install lint typecheck test demo check

install:
	python3 -m venv .venv
	$(PYTHON) -m pip install -e '.[dev,demo]'

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

typecheck:
	$(PYTHON) -m mypy --strict src

test:
	$(PYTHON) -m pytest -q

demo:
	$(PYTHON) scripts/demo.py

check: lint typecheck test
