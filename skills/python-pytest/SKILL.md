---
name: python-pytest
description: Reproducing and verifying Python bugs with pytest. Use when the repository has pytest tests or a pyproject/setup.cfg.
---
# Python bugs with pytest

## Reproduce
- Prefer a failing test case over a script when the project already has tests for the module: add one
  `test_...` function next to the related tests. It doubles as the regression test.
- Run one test fast: `python -m pytest -q -p no:cacheprovider path/to/test_file.py -k name -x`.
- If collection fails with ImportError, the package is probably not installed: `python -m pip install -e .`
  (or `pip install -r requirements.txt`) and rerun.

## Fix
- Read the whole function and its callers before editing; search for other call sites with `search`.
- Keep public signatures unchanged unless the issue asks otherwise.

## Verify
- Run the new test, then the test file, then the whole suite (`run_tests`).
- A test that failed before your change and passes after is the proof to cite in `finish`.
- Never loosen an assertion or add `skip`/`xfail` to make a test pass.
