# Trojan Horse

Trojan Horse is a coding-agent harness for text-only models. It takes a repository and an issue, finds the relevant code, reproduces the bug, fixes it, and then checks the fix itself before it reports success. It was built for DeepSeek and Qwen models behind any OpenAI-compatible API.

The model does the reasoning. Trojan Horse decides what the model sees, what it is allowed to do, and when the work counts as done.

## Quick start

```bash
export AI_API_KEY="<key>"
make setup
make run
```

`make run` opens a terminal UI. Paste a GitHub issue URL (the repository is cloned automatically), or give a repository path or git URL plus the issue text, then press ctrl+s.

To skip the form:

```bash
make run REPO=https://github.com/owner/repo ISSUE=https://github.com/owner/repo/issues/42
make run REPO=/path/to/repo ISSUE=@issue.md
make headless REPO=/path/to/repo ISSUE=@issue.md     # plain output, no UI
```

When `make run` has no terminal attached (for example in a script), it runs headless automatically and also accepts the issue on stdin.

| Command | What it does |
|---|---|
| `make setup` | Creates `.venv` and installs everything (uses `uv` when available, otherwise `venv` and `pip`) |
| `make run` | Launches the UI, or starts a run directly when `REPO` and `ISSUE` are given |
| `make test` | Runs the offline test suite. No API key needed |
| `make bench` | Runs the agent on the bundled buggy repositories and scores it with hidden tests |
| `make doctor` | Checks the key, shows the detected provider and model, and tests tool calling |
| `make replay` | Replays the last run in the UI without calling the model |
| `make clean` | Removes the venv, run logs and cloned repositories |

Requirements: Python 3.9 or newer, and git. Node is only needed for JavaScript repositories.

## How a run works

```
issue ──► localise ──► agent loop ──► finish gate ──► harness verification ──► reviewer ──► report
            │             │               │                  │                    │
       grep the issue's   tools: read,    needs a diff and   repro fails on the   reads the diff
       identifiers,       search, edit,   a test run after   original code and    against the issue
       rank files         bash, tests     the last edit      passes on the fix
```

1. **Localise.** Before the first model call, Trojan Horse pulls identifiers, file paths and traceback frames out of the issue and greps the repository for them. Rare terms count for more than common ones, and test files and docs rank lower. The model starts with a ranked list of likely files and an outline of the top ones, which saves several exploration turns.
2. **Agent loop.** The model works through a fixed workflow (understand, locate, reproduce, fix, verify) with twelve tools. It writes a reproduction script in a scratch directory outside the repo, so scratch files never end up in the patch.
3. **Finish gate.** The model cannot simply declare victory. `finish` is rejected if nothing changed, or if files were edited after the last test run.
4. **Harness verification.** Trojan Horse reruns the model's reproduction command twice: on a clean checkout of the original code, where it must fail, and on the fixed code, where it must pass. It also runs the test suite and compares against the original code, so failures that were already there are not blamed on the fix. Any test files the agent added are copied into the original checkout first, so "fails before" is a real failure and not a missing file.
5. **Reviewer.** A separate model call reads the issue and the final diff and either approves it or sends back specific problems.
6. **Second attempt, only when needed.** If an attempt ends unverified, Trojan Horse resets the tree and starts again with a note about what went wrong. The best patch across attempts is kept. A run that is verified the first time costs nothing extra.
7. **Report.** Every run writes `runs/<timestamp>-<repo>/` with `report.md` (summary, evidence table, patch), `patch.diff`, `summary.json`, `trajectory.jsonl` (every step), and `outputs/` (full tool output).

The patch always comes from `git diff` against the starting state. The model is never asked to write a diff.

## Tools

| Tool | Notes |
|---|---|
| `bash` | Runs in the repo root with no stdin and a timeout. Blocks `git push`, `sudo`, `rm -rf /`, history-destroying git commands and interactive programs. The API key is removed from its environment. |
| `read_file` | Numbered lines, 250 at a time |
| `search` | Regex or literal search, capped at 50 results with a per-file count of the rest (ripgrep when installed, otherwise pure Python) |
| `find_files`, `list_dir` | Skip dependency and build folders |
| `edit_file` | Exact string replacement. When the text does not match, it retries while ignoring whitespace differences and fixes the indentation of the new lines. When there is still no match, it shows the closest region of the file. Copied line-number prefixes are stripped. |
| `write_file`, `undo_edit`, `git_diff` | |
| `run_tests` | Uses the detected test command (pytest, npm/yarn/pnpm, `node --test`, go, cargo, maven, gradle, `make test`) and summarises pass/fail counts |
| `update_plan` | The plan is shown live in the UI |
| `finish` | Takes a summary and the reproduction command |

## Keeping a model on track

These came from watching real runs of a small local model and fixing what went wrong:

- An edit whose `new_str` is already in the file is answered with "this change is already applied, move on to verification" instead of a confusing "not found" error.
- A reproduction script that exits 0 before any fix gets flagged: it does not detect the bug, and it should assert the expected behaviour.
- On the third identical tool call, the reply includes the current diff and asks for a different next step.
- Reads shorter than 100 lines are widened, so the model does not spend a turn per 15-line slice.
- If the model stops calling tools, it is nudged. After three replies with no tool call, the last reply is treated as a request to finish, which then goes through the normal finish checks.

Every edit to a Python, JSON, TOML or JavaScript file is syntax-checked immediately, and an edit that breaks a file that was valid before is rolled back with the error message. Edits to existing test files are flagged to the model, the reviewer and the report.

## Models and providers

The organisers supply the key in `AI_API_KEY`. Trojan Horse works out which provider issued it by probing DeepSeek, Alibaba DashScope (international, US and China), Amazon Bedrock (its OpenAI-compatible endpoint; keys start with `ABSK`), OpenRouter, SiliconFlow and Together in parallel, then picks a model from the preference list in `config/harness.toml` that the endpoint actually serves. Nothing needs editing when the model or provider changes:

```bash
export AI_PROVIDER=dashscope-intl          # pin a provider from the config
export AI_BASE_URL=https://host/v1         # or any OpenAI-compatible endpoint
export AI_MODEL=qwen3-coder-next           # pin the model
```

Things the harness handles so the model does not have to:

- DeepSeek's thinking mode requires `reasoning_content` to be sent back during a tool loop, or the API returns a 400 error. Trojan Horse keeps it in the history for DeepSeek and drops it for other providers.
- Both model families sometimes write tool calls into the message text instead of the tool-call field: DeepSeek in its DSML markup or older special tokens, Qwen as `<tool_call><function=...>` XML. These are parsed and executed. Tool syntax inside code fences is ignored.
- Malformed argument JSON is repaired where possible (trailing commas, truncation, a stray `arguments` wrapper). Otherwise the model gets a precise error.
- If an endpoint rejects the `tools` parameter, Trojan Horse switches to a text tool-call format and carries on.
- Rate limits and server errors are retried with backoff and `Retry-After`. A "prompt too long" error triggers compaction and a retry.
- Replies are streamed. A request is only abandoned when the model goes silent for four minutes, so a long thinking phase is never cut off and regenerated from scratch. The UI shows how much the model has written while it thinks.

For testing with free keys only, `AI_FALLBACK_MODELS=model-a,model-b` lets a run continue on the next model when the current one's daily quota runs out (free Gemini keys allow about 20 requests per model per day). It is off unless set, since the evaluation rules forbid replacing the prescribed model.

Runs are deterministic where the API allows it: temperature 0 (ignored by thinking models) and a fixed seed.

## Token efficiency

- Tool output is cut to about 9,000 characters (a third from the head, the rest from the tail) and the full text is saved to disk with its path given to the model.
- The system prompt and task message are fixed text and the history is append-only, so providers with prefix caching, such as DeepSeek, bill most of each request at the cached rate. The UI and report show the cache-hit percentage.
- When the prompt passes 80% of the context budget, old tool outputs are masked in a single batch. If that is not enough, the oldest turns are replaced by a short state note (plan, files edited, last command). Doing this rarely and all at once keeps the cached prefix valid for as long as possible.
- There are hard limits on steps and total tokens, and the UI shows a live meter: requests, tokens in and out, cached tokens and cost.

## Configuration

All settings are in `config/harness.toml`: providers and model preferences, sampling, step and token limits, context budget, tool timeouts, reviewer and retry settings, and prices for the cost meter. The API key is never stored in the repository.

## Tests and benchmark

`make test` runs the offline suite (49 tests) against a scripted fake model server. It covers the tool-call parsers, the editor's fallbacks and syntax guard, command blocking, key hiding, provider selection, retries, the finish gate, harness verification, the reviewer round-trip, the second attempt, and a full run through the UI.

`bench/` holds five small repositories with planted bugs: four Python and one JavaScript, easy to hard. Each has an issue written like a real bug report and a hidden test the agent never sees. `make bench` runs Trojan Horse on each one and scores it with the hidden tests. See `bench/README.md`.

### Results so far

| Model | Task | Hidden test | Harness verdict | Steps | Tokens in / out |
|---|---|---|---|---|---|
| Qwen 3.8 27B (OpenRouter free tier) | py-csv-quotes (medium) | pass | verified: repro fails before, passes after; 19 tests pass; reviewer approved | 9 | 57.8K (39% cached) / 10.7K |
| Qwen 3.8 27B (OpenRouter free tier) | py-pagination (easy) | pass (11/11) | verified: 18 tests pass; reviewer approved. The model's own repro did not fail on the original code, so the report does not claim "bug proven" | 11 requests | 78.3K (75% cached) / 3.8K |
| Qwen 3.8 27B (OpenRouter free tier) | js-semver-range (medium, JavaScript) | pass | verified: node tests pass; the model's repro only printed, so no "bug proven" claim | 9 | 83.7K (63% cached) / 5.2K |
| Qwen 3.8 27B (OpenRouter free tier) | py-ttl-cache (hard) | pass | verified: repro fails before, passes after; 18 tests pass | 7 | 43.5K (54% cached) / 1.9K |
| Qwen3 8B (local, Ollama) | py-pagination (easy) | pass | unverified: the model's own reproduction asserted the wrong behaviour | 56 | 345K / 4.4K |
| Qwen3 8B (local, Ollama) | py-csv-quotes (medium) | fail | unverified: near-miss edits never applied | 69 | 497K / 4.8K |

**A real open-source issue.** On [shouri123/Late-Meet#778](https://github.com/shouri123/Late-Meet/issues/778), an open GSSoC bug in a TypeScript Chrome extension (recording stays paused forever because `AudioChunkQueue` misses its `onDrain` callback), Trojan Horse on Gemini Flash produced a verified fix in 4 minutes 41 seconds: an 11-line change plus a regression test that fails on the original code and passes on the fix. The repository's 133 existing tests, its type check and its lint all pass, and an independent reproduction plus 7 edge-case checks (6 pass; the seventh concerns `clear()`, which the app only calls during a full session reset) were run by hand afterwards. That run also exposed a harness flaw, since fixed: the original-code checkout had no `node_modules`, so "command not found" was briefly counted as the bug reproducing.

The 8B runs are where most of the steering in "Keeping a model on track" came from. In every case the harness verdict matched reality: it never reported a fix as verified when the hidden test failed.

The verified Qwen 3.8 run is saved in `examples/`, so `make replay` shows a real run even on a fresh clone with no key.

While the model thinks, the run screen shows a spinner with a rotating word (Percolating, Cogitating, Galumphing, Sneaking past the walls…), how long it has been thinking and how much it has written; while a tool runs, it shows what is running. The words live in `trojan/spinner.py` if you want to add your own.

## Approval modes

Like Codex and Claude Code, Trojan Horse can run hands-off or ask first.

- **auto-approve** (default): every tool call runs without asking. This is what unattended evaluation needs.
- **ask before changes**: before each shell command or file edit, a prompt shows the exact command or the diff about to be applied. Press `y` to approve, `a` to approve everything from then on, or `n` to reject, optionally with a note that is passed back to the agent. Reading and searching never ask.

Switch with `ctrl+t` in the UI (it works mid-run too), with `--approval ask` on the command line, or with `approval = "ask"` in `config/harness.toml`. Headless runs ask on the terminal with the same y / n / a keys. The harness's own verification (rerunning the reproduction and tests on the original and fixed code) runs without prompts, since it only checks work that was already approved.

## Skills

A skill is reusable know-how the agent can load when it fits the task: a folder with a `SKILL.md` that has a name, a one-line description and instructions. The agent only sees the one-line descriptions (a few tokens each) and loads the full text with the `use_skill` tool when it decides a skill is relevant.

Skills are picked up from three places, later ones overriding earlier ones with the same name:

| Folder | Scope |
|---|---|
| `skills/` in this repository | built in: `python-pytest`, `node-testing`, `async-race-bugs` |
| `~/.trojan/skills/` | your own, for every repository |
| `.trojan/skills/` inside the target repository | that project's conventions |

```bash
make skill NAME=django-migrations    # creates ~/.trojan/skills/django-migrations/SKILL.md from a template
make skills                          # lists every skill found
```

A skill looks like this:

```markdown
---
name: django-migrations
description: Creating and checking Django migrations. Use when a fix changes a model.
---
Run `python manage.py makemigrations --check` before finishing ...
```

## Choosing a model in the UI

Press `ctrl+o` on the launch screen to pick a model. The list shows the models your `AI_API_KEY` can use and, if Ollama is running, every local model on the machine. Local models are loaded with a 32K context automatically (Ollama's default of 4K would cut the prompt). The launch screen and the cost meter also show the credit left on the key where the provider reports it (OpenRouter, DeepSeek).

## Local development without a key

Any OpenAI-compatible local server works. With Ollama, create a variant with a larger context, since the default of 4,096 tokens truncates the prompt:

```bash
printf 'FROM qwen3:8b\nPARAMETER num_ctx 32768\n' > Modelfile
ollama create trojan-qwen3 -f Modelfile
AI_PROVIDER=ollama AI_MODEL=trojan-qwen3 make run
```

The Ollama provider is never picked automatically, so it cannot replace the prescribed model during evaluation.

## Layout

```
Makefile              standard interface: setup, run, test, bench, doctor, replay, clean
config/harness.toml   models, providers, limits, prices
trojan/
  llm.py              OpenAI-compatible client, provider detection, retries, token accounting
  toolparse.py        recovery of tool calls written as text; argument JSON repair
  tools.py            the agent's tools, the editor and the syntax guard
  workspace.py        repo preparation, sandboxed commands, test detection, git-based diffs
  localize.py         keyword-based file ranking and outlines
  context.py          cache-friendly context compaction
  agent.py            the loop, finish gate, verification, retries
  reviewer.py         the review pass
  report.py           run reports
  tui.py              the terminal UI
  runner.py           shared glue for the CLI and UI, and replay
tests/                offline test suite
bench/                buggy repositories with hidden tests
```
