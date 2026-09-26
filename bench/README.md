# bench

Small SWE-bench style tasks for evaluating the harness. Each task is a
self-contained project with one planted bug, an issue describing the bug from
a user's point of view, and a hidden test that fails on the buggy code and
passes once the bug is fixed.

## Layout

```
bench/
  validate_tasks.py        checks that every task is well formed
  tasks/<id>/
    repo/                  the project the agent works on (stdlib only)
    issue.md               the bug report given to the agent
    hidden_test.py|.mjs    fail-to-pass test, never shown to the agent
    solution.patch         reference fix, relative to repo/ (git apply)
    meta.json              id, language, difficulty, test commands, description
```

`meta.json` fields:

| field              | meaning                                                         |
|--------------------|-----------------------------------------------------------------|
| `id`               | same as the directory name                                      |
| `language`         | `python` or `javascript`                                        |
| `difficulty`       | `easy`, `medium` or `hard`                                      |
| `visible_test_cmd` | runs the repo's own tests from the repo root; passes before and after the fix |
| `hidden_test_cmd`  | runs the hidden test after it is copied into the repo root      |
| `description`      | one line, for reports                                           |

## Tasks

| id                | lang       | difficulty | bug                                                                  |
|-------------------|------------|------------|----------------------------------------------------------------------|
| `py-pagination`   | python     | easy       | page count is one too high when the item count divides evenly        |
| `py-config-merge` | python     | medium     | nested settings overrides drop sibling defaults (symptom shows via `load_settings`) |
| `py-csv-quotes`   | python     | medium     | reader fails on doubled quotes inside quoted fields                  |
| `py-ttl-cache`    | python     | hard       | re-setting a live key keeps its old TTL and LRU position             |
| `js-semver-range` | javascript | medium     | `^0.0.x` caret ranges allow later patch versions                     |

## Running a task against the harness

1. Copy `tasks/<id>/repo/` to a scratch directory and give the agent that
   directory plus the contents of `issue.md`. Do not expose the task
   directory itself, since it contains the hidden test and the fix.
2. When the agent finishes, copy `hidden_test.py` (or `.mjs`) into the root of
   the agent's working copy and run `hidden_test_cmd` there. Exit code 0
   means the task is resolved.
3. Run `visible_test_cmd` as well to check that nothing else broke.

Python tasks need `pytest`. The JS task needs Node 18 or newer and no
packages.

## Validating the tasks

```
python3 bench/validate_tasks.py            # all tasks
python3 bench/validate_tasks.py --task py-ttl-cache --keep -v
python3 bench/validate_tasks.py --python .venv/bin/python
```

For each task the script copies `repo/` to a temp dir, commits it to a fresh
git repo, and checks that the visible tests pass, the hidden test fails,
`solution.patch` applies with `git apply`, and then both the hidden and
visible tests pass. A hidden test that errors during collection (pytest exit
code other than 1) counts as a broken task, not as a failing test.

Commands starting with `python` or `python3` are run with the interpreter
passed via `--python`, which defaults to the one running the script. That
interpreter must have pytest installed; the script stops early if it does
not.

## Adding a task

Write the project under `repo/` with tests that pass on the buggy code, then
generate the patch from a scratch copy:

```
cp -R bench/tasks/<id>/repo /tmp/t && cd /tmp/t
git init -q && git add -A && git commit -qm base
# edit the fix
git diff > <path to>/bench/tasks/<id>/solution.patch
```

Then run `validate_tasks.py --task <id>`.
