# Trojan Horse: standard evaluation interface.
#
#   export AI_API_KEY="<key>"
#   make setup
#   make run                                   # TUI: paste a repo and an issue
#   make run REPO=<path|git url> ISSUE=<text|@file|github issue url>
#   make test
#
# The key is read from the environment only. Recipes never print it.

SHELL  := /bin/bash
PYTHON ?= python3
VENV   := .venv
PY     := $(VENV)/bin/python
REPO   ?=
ISSUE  ?=
ARGS   ?=

.PHONY: help setup run headless test bench doctor replay skills skill clean

help:
	@echo "make setup     install everything into $(VENV)"
	@echo "make run       launch the Trojan Horse TUI (REPO=... ISSUE=... to start immediately)"
	@echo "make headless  run without the TUI (needs REPO and ISSUE)"
	@echo "make test      offline test suite (no API key needed)"
	@echo "make bench     live benchmark on the bundled buggy repos (needs AI_API_KEY)"
	@echo "make doctor    check the key, provider, model and tool calling"
	@echo "make replay    replay the latest run without calling the model"
	@echo "make skills    list installed skills; make skill NAME=x creates a new one"
	@echo "make clean     remove the venv, runs and cloned workspaces"

setup:
	@echo "Setting up Trojan Horse..."
	@command -v git >/dev/null 2>&1 || { echo "git is required but was not found"; exit 1; }
	@$(PYTHON) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
		|| { echo "Python 3.9 or newer is required (found: $$($(PYTHON) --version 2>&1))"; exit 1; }
	@if command -v uv >/dev/null 2>&1; then \
		[ -x "$(PY)" ] || uv venv -q --python "$$(command -v $(PYTHON))" $(VENV); \
		uv pip install -q --python $(PY) -e ".[dev]"; \
	else \
		[ -x "$(PY)" ] || $(PYTHON) -m venv $(VENV); \
		$(PY) -m pip install -q --upgrade pip; \
		$(PY) -m pip install -q -e ".[dev]"; \
	fi
	@$(PY) -m trojan --version
	@if [ -z "$${AI_API_KEY}" ]; then echo "Note: AI_API_KEY is not set yet. Run: export AI_API_KEY=\"<key>\""; fi
	@echo "Setup complete. Next: make run"

run:
	@[ -x "$(PY)" ] || { echo "Run 'make setup' first."; exit 1; }
	@$(PY) -m trojan $(if $(REPO),--repo "$(REPO)") $(if $(ISSUE),--issue "$(ISSUE)") $(ARGS)

headless:
	@$(PY) -m trojan --headless $(if $(REPO),--repo "$(REPO)") $(if $(ISSUE),--issue "$(ISSUE)") $(ARGS)

test:
	@echo "Running the offline test suite..."
	@$(PY) -m pytest -q tests
	@echo "Offline tests passed. For a live run on the bundled buggy repos: make bench"

bench:
	@$(PY) bench/run_bench.py $(ARGS)

doctor:
	@$(PY) -m trojan --check

replay:
	@$(PY) -m trojan --replay $(ARGS)

skills:
	@$(PY) -m trojan --skills

skill:
	@[ -n "$(NAME)" ] || { echo "Usage: make skill NAME=my-skill"; exit 2; }
	@$(PY) -m trojan --new-skill "$(NAME)"

clean:
	@rm -rf $(VENV) runs workspace bench/results *.egg-info .pytest_cache
	@find . -name __pycache__ -type d -prune -exec rm -rf {} +
	@echo "Cleaned."
