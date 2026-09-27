# Trojan Horse

Trojan Horse is a coding-agent harness for text-only models. Give it a repository and an issue. It finds the relevant code, reproduces the bug, fixes it, and checks the fix itself before it reports success. We built it for DeepSeek and Qwen models behind any OpenAI-compatible API. It also runs on Gemini, Amazon Bedrock and local Ollama models.

The model does the reasoning, and Trojan Horse decides when the work counts as done. The model can be wrong, but it can't call a fix done without proof that a failing check now passes.

## Quick start

```bash
export AI_API_KEY="<key>"
make setup
make run
```

`make run` opens the terminal UI. Paste a GitHub issue link and press Enter, and Trojan Horse clones the repository for you. You can also put a local path or git URL on the `repo` line and describe the bug on the `>` line.

To start a run straight away:

```bash
make run REPO=https://github.com/owner/repo ISSUE=https://github.com/owner/repo/issues/42
make run REPO=/path/to/repo ISSUE=@issue.md
make headless REPO=/path/to/repo ISSUE=@issue.md     # plain output, no UI
```

Without a terminal attached, as in a script, `make run` falls back to headless mode and also reads the issue from stdin.

| Command | What it does |
|---|---|
| `make setup` | Creates `.venv` and installs everything, with `uv` if it's installed and `venv` plus `pip` otherwise |
| `make run` | Opens the UI, or starts a run directly when `REPO` and `ISSUE` are given |
| `make headless` | Runs without the UI |
| `make test` | Runs the offline test suite. No API key needed |
| `make bench` | Runs the agent on the bundled buggy repositories and scores it with hidden tests |
| `make doctor` | Checks the key, shows the provider and model it picked, and tests tool calling |
| `make replay` | Replays the last run in the UI without calling the model |
| `make skills`, `make skill NAME=x` | Lists skills, or creates a new one from a template |
| `make clean` | Removes the venv, run logs and cloned repositories |

You need Python 3.9 or newer and git. JavaScript repositories also need Node.

## The terminal UI

The launch screen shows the horse, the version, the model Trojan Horse connected to, the credit left on the key when the provider reports it, the repository, how many skills it loaded, and the prompt.

| Key | Where | What it does |
|---|---|---|
| `enter` | launch screen, follow-up prompt | Starts the run |
| `tab` / `shift+tab` | launch screen | Moves between the `repo` and `>` lines |
| `ctrl+j` | any prompt | Adds a new line |
| `ctrl+o` | launch screen | Picks the model from your key's models and every local Ollama model |
| `ctrl+t` | anywhere, even mid-run | Switches between auto-approve and ask before changes |
| `ctrl+r` | launch screen | Replays the last run |
| `ctrl+n` | after a run | Goes back to the launch screen for a new task |
| `d`, `c`, `q` | run screen | Shows the diff, cancels the run, quits |
| `?` | empty prompt | Shows every shortcut |

The run screen streams each step on the left. The right side shows the status, a cost meter, the plan, the files changed, and the evidence. The cost meter counts requests, tokens in and out, the cached share, cost, credit left and time. The evidence panel says whether the reproduction failed on the original code and passes on the fix, with the test counts. While the model thinks, a spinner shows a rotating word, how long it has been thinking and how much it has written. Percolating, Galumphing, Sneaking past the walls and the rest live in `trojan/spinner.py`.

When a run finishes, a prompt opens under the results. Type a follow-up such as "also handle the empty list" and Trojan Horse runs it on the same repository, on top of the changes it just made. It tells the agent what the previous task was and what changed, and the new diff and checks cover only the new work. A GitHub issue link or `@file` in that prompt starts an unrelated task on the same repository.

### Approval modes

Trojan Horse can run hands-off or ask first, the way Codex and Claude Code do. There are two modes.

- Auto-approve is the default. Every tool call runs without asking, which unattended evaluation needs.
- Ask before changes stops before each shell command or file edit and shows the exact command or the diff. Press `y` to approve, `a` to approve everything after this, or `n` to reject. After `n` you can type a note, and the agent reads it. Reading and searching never ask.

Switch with `ctrl+t`, with `--approval ask` on the command line, or with `approval = "ask"` in `config/harness.toml`. Headless runs ask on the terminal with the same keys. Trojan Horse's own verification never prompts, because it only re-checks work you already approved.

## How a run works

```
issue ──► localise ──► agent loop ──► finish gate ──► verification ──► reviewer ──► report
            │             │               │                  │                    │
       grep the issue's   tools: read,    needs a diff and   repro fails on the   reads the diff
       identifiers,       search, edit,   a test run after   original code and    against the issue
       rank files         bash, tests     the last edit      passes on the fix
```

1. Localise. Before the first model call, Trojan Horse pulls identifiers, file paths and traceback frames out of the issue and greps the repository for them. Rare terms count for more than common ones. It ignores words inside string literals in the issue's examples and ranks test files and docs lower. Modules that the best matches import get part of their score, because bugs often sit one call below the symptom. The model starts with this ranked list and an outline of the top files.
2. Agent loop. The model works in a fixed order with 13 tools. It understands the issue, finds the code, reproduces the bug, fixes it and verifies the fix. Reproduction scripts go in a scratch folder outside the repository, so they never end up in the patch.
3. Finish gate. Trojan Horse rejects `finish` when nothing changed, or when the model edited files after its last test run. It accepts "no change needed" only with a reproduction that passes on the untouched code.
4. Verification. Trojan Horse reruns the model's reproduction on a clean checkout of the original code, where it must fail, and on the fixed code, where it must pass. It links the installed dependencies into that checkout and copies in any tests the agent added, so a failure there is a real one and not a missing file or tool. It also runs the test suite on both, so it never blames the fix for failures that were already there.
5. Reviewer. A separate model call reads the issue and the final diff, then approves it or sends back specific problems.
6. Second attempt, only when needed. When an attempt ends unverified, Trojan Horse resets the tree and starts again with a note on what went wrong. It keeps the better patch. A run verified the first time costs nothing extra.
7. Report. Each run writes `runs/<timestamp>-<repo>/`. It holds `report.md` with the summary, evidence table and patch, plus `patch.diff`, `summary.json`, `trajectory.jsonl` with every step, and `outputs/` with full tool output.

The patch always comes from `git diff` against the starting state. Nobody asks the model to write a diff.

## Tools

| Tool | Notes |
|---|---|
| `bash` | Runs in the repo root with no stdin and a timeout. Blocks `git push`, `sudo`, `rm -rf /`, history-destroying git commands and interactive programs. The API key is removed from its environment |
| `read_file` | Numbered lines, 250 at a time. Requests under 100 lines grow to 100 |
| `search` | Regex or literal search, capped at 50 results with a per-file count of the rest. Uses ripgrep when installed, otherwise pure Python |
| `find_files`, `list_dir` | Skip dependency and build folders |
| `edit_file` | Exact string replacement. On a mismatch it retries while ignoring whitespace and fixes the indentation of the new lines. If nothing matches, it shows the closest region, which lines differ, and the exact text to use. It strips copied line numbers |
| `write_file`, `undo_edit`, `git_diff` | |
| `run_tests` | Runs the detected test command and summarises the counts. Knows pytest, npm, yarn, pnpm, `node --test`, go, cargo, maven, gradle and `make test` |
| `use_skill` | Loads a skill's full instructions |
| `update_plan` | Shows the plan live in the UI |
| `finish` | Takes a summary and the reproduction command |

Trojan Horse syntax-checks every edit to a Python, JSON, TOML or JavaScript file on the spot. If an edit breaks a file that was valid before, it rolls the edit back and shows the error. It flags edits to existing test files to the model, the reviewer and the report.

## Keeping a model on track

We wrote these rules after watching real runs go wrong:

- An edit whose `new_str` is already in the file gets "this change is already applied, move on to verification" instead of a confusing "not found".
- A reproduction script that exits 0 before any fix gets flagged. It doesn't detect the bug yet, so it should assert the expected behaviour.
- On the third identical tool call, the reply includes the current diff and asks for a different next step.
- When a reproduction still fails after the fix, the model hears whether that points at the fix or at the script. A script that fails the same way before and after is probably wrong itself.
- A model that stops calling tools gets a nudge. After three replies with no tool call, Trojan Horse treats the last one as a request to finish, which goes through the usual checks.
- If Trojan Horse can't fetch a GitHub issue, it stops with a clear message instead of guessing. It retries first, then tries the `gh` CLI.

## Skills

A skill is a folder with a `SKILL.md` that holds a name, a one-line description and instructions. The agent sees only the descriptions, a few tokens each, and loads the full text with `use_skill` when a skill applies. On the Late-Meet issue below, it loaded `async-race-bugs` on its first step, before reading any code.

Trojan Horse reads skills from three places. A later one overrides an earlier one with the same name.

| Folder | Scope |
|---|---|
| `skills/` in this repository | Built in: `python-pytest`, `node-testing`, `async-race-bugs` |
| `~/.trojan/skills/` | Yours, for every repository |
| `.trojan/skills/` in the target repository | That project's own conventions |

```bash
make skill NAME=django-migrations    # creates ~/.trojan/skills/django-migrations/SKILL.md from a template
make skills                          # lists every skill found
```

```markdown
---
name: django-migrations
description: Creating and checking Django migrations. Use when a fix changes a model.
---
Run `python manage.py makemigrations --check` before finishing ...
```

## Models and providers

Trojan Horse reads the key from `AI_API_KEY` and works out who issued it. It probes DeepSeek, Alibaba DashScope in its international, US and China regions, Amazon Bedrock, OpenRouter, Gemini, SiliconFlow and Together in parallel. Bedrock keys start with `ABSK`. It then picks the first model from the preference list in `config/harness.toml` that the endpoint actually serves, and a free-tier OpenRouter key gets a free model. Nothing needs editing when the model or provider changes.

```bash
export AI_PROVIDER=dashscope-intl          # pin a provider from the config
export AI_BASE_URL=https://host/v1         # or use any OpenAI-compatible endpoint
export AI_MODEL=qwen3-coder-next           # pin the model
```

For local use, the key can also sit in a `.env` file in the project folder, which git ignores. A key set in the environment always wins.

Provider quirks Trojan Horse handles:

- DeepSeek's thinking mode returns 400 unless the tool loop sends `reasoning_content` back. Trojan Horse keeps it for DeepSeek and drops it for other providers. It passes Gemini's thought signatures on tool calls back untouched for the same reason.
- DeepSeek and Qwen sometimes write tool calls into the text instead of the tool-call field. DeepSeek uses its DSML markup or older special tokens, and Qwen uses `<tool_call><function=...>` XML. Trojan Horse parses and runs these, and ignores tool syntax inside code fences.
- It repairs broken argument JSON where it can, such as trailing commas, truncation or a stray `arguments` wrapper. Otherwise the model gets a precise error.
- If an endpoint rejects `tools`, Trojan Horse switches to a text tool-call format and carries on.
- It streams replies and drops a request only after four minutes of silence, so it never cuts off a long thinking phase and pays for it twice.
- It retries server errors with backoff and honours `Retry-After`. It waits out a busy provider returning HTTP 429 for up to about 15 minutes instead of burning the attempt. A "prompt too long" error triggers compaction and a retry.
- Sampling follows each vendor's advice. DeepSeek runs at temperature 0. Qwen runs at 0.7 with top_p 0.8, since greedy decoding makes it repeat itself. The seed is fixed either way.

For testing with free keys only, `AI_FALLBACK_MODELS=model-a,model-b` moves a run on to the next model when the current one's daily quota runs out. Free Gemini keys allow about 20 requests per model per day. It stays off unless set, because the evaluation rules forbid replacing the prescribed model.

### Local models

Press `ctrl+o` and pick any model Ollama serves. Ollama loads most models with a 4K context, which cuts the prompt. So the first time you pick a local model, Trojan Horse creates a copy with a 32K context and uses that. From the command line:

```bash
AI_PROVIDER=ollama AI_MODEL=qwen3:8b make run
```

Trojan Horse never picks the Ollama provider on its own, so a local model can't replace the prescribed one during evaluation.

## Token efficiency

- Trojan Horse cuts tool output to about 9,000 characters, a third from the start and the rest from the end. It saves the full text to disk and gives the model the path.
- The system prompt and task message never change and the history only grows at the end. Providers with prefix caching, such as DeepSeek, bill most of each request at the cached rate. The UI and report show the cached share.
- Past 80% of the context budget, it swaps old tool outputs for short stubs in one batch. If that isn't enough, it turns the oldest turns into a short note with the plan, the files edited and the last command. Doing this rarely, and all at once, keeps the cached prefix valid for as long as possible.
- A skill costs one line until the agent loads it.
- Steps and total tokens have hard limits, and the UI shows the live meter.

## Configuration

`config/harness.toml` holds everything: providers and model preferences, sampling, step and token limits, the context budget, tool timeouts, the reviewer, retries, the approval mode, and prices for the cost meter. The API key never goes in the repository.

## Tests, benchmark and results

`make test` runs 55 offline tests against a scripted fake model server. They cover the tool-call parsers, the editor's fallbacks and syntax guard, command blocking, key hiding, provider selection, streaming, retries and fallbacks, the finish gate, verification, the reviewer, second attempts, skills, approvals, follow-up tasks, and full runs through the UI.

`bench/` holds five small repositories with planted bugs, four in Python and one in JavaScript. One is easy, three are medium and one is hard. Each comes with an issue written like a real bug report and a hidden test the agent never sees. `make bench` runs Trojan Horse on each one and scores it with the hidden tests.

| Model | Task | Hidden test | Trojan Horse's verdict | Steps | Tokens in / out |
|---|---|---|---|---|---|
| Qwen 3.8 27B, OpenRouter free tier | py-csv-quotes, medium | pass | verified. Repro fails before, passes after. 19 tests pass. Reviewer approved | 9 | 57.8K, 39% cached / 10.7K |
| Qwen 3.8 27B, OpenRouter free tier | py-pagination, easy | pass, 11/11 | verified. 18 tests pass. Reviewer approved. The model's repro didn't fail on the original code, so the report doesn't claim "bug proven" | 11 | 78.3K, 75% cached / 3.8K |
| Qwen 3.8 27B, OpenRouter free tier | js-semver-range, medium, JavaScript | pass | verified. Tests pass. The repro only printed, so no "bug proven" claim | 9 | 83.7K, 63% cached / 5.2K |
| Qwen 3.8 27B, OpenRouter free tier | py-ttl-cache, hard | pass | verified. Repro fails before, passes after. 18 tests pass | 7 | 43.5K, 54% cached / 1.9K |
| Qwen3 8B, local on Ollama | py-pagination, easy | pass | unverified. The model's reproduction asserted the wrong behaviour | 56 | 345K / 4.4K |
| Qwen3 8B, local on Ollama | py-csv-quotes, medium | fail | unverified. Its near-miss edits never applied | 69 | 497K / 4.8K |

In every run, Trojan Horse's verdict matched the hidden test. Trojan Horse never called a fix verified when the hidden test failed. Most of the rules under "Keeping a model on track" came out of the 8B runs.

### A real open-source issue

[shouri123/Late-Meet#778](https://github.com/shouri123/Late-Meet/issues/778) is an open GSSoC bug in a TypeScript Chrome extension. Recording stays paused for good because `AudioChunkQueue` misses its `onDrain` callback. Running on Gemini Flash, Trojan Horse loaded the `async-race-bugs` skill, wrote a regression test first, and produced a verified fix in 4 minutes 41 seconds. The fix is an 11-line change plus the test, which fails on the original code and passes on the fix. The repository's 133 existing tests, its type check and its lint all pass.

We then reviewed the fix by hand with a separate reproduction and 7 edge cases. Six pass, and the seventh concerns `clear()`, which the app only calls during a full session reset, so it doesn't matter in practice. The same run exposed a flaw in Trojan Horse, since fixed. The checkout of the original code had no `node_modules`, so "command not found" briefly counted as the bug reproducing.

The verified Qwen 3.8 run lives in `examples/`, so `make replay` shows a real run on a fresh clone with no key.

## Layout

```
Makefile              standard interface: setup, run, test, bench, doctor, replay, skills, clean
config/harness.toml   providers, models, sampling, limits, approval mode, prices
trojan/
  llm.py              OpenAI-compatible client: provider detection, streaming, retries, token accounting
  toolparse.py        recovery of tool calls written as text; argument JSON repair
  tools.py            the agent's tools, the editor, the syntax guard and the approval hook
  workspace.py        repo preparation, sandboxed commands, test detection, git-based diffs
  localize.py         keyword and import-graph file ranking, outlines
  context.py          cache-friendly context compaction
  agent.py            the loop, finish gate, verification, retries
  skills.py           skill discovery and templates
  reviewer.py         the review pass
  report.py           run reports
  issue.py            issue loading from text, files and GitHub
  tui.py              the terminal UI
  logo.py, spinner.py the horse and the thinking words
  runner.py           shared glue for the CLI and UI, and replay
skills/               built-in skills
tests/                offline test suite
bench/                buggy repositories with hidden tests
examples/             a recorded run for make replay
```
