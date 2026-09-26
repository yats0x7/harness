# Wrench

An autonomous coding-agent harness for text-only models (DeepSeek and Qwen). Give it a repository and an issue; it finds the code, reproduces the bug, fixes it, and proves the fix works before it stops.

## Run it

```bash
export AI_API_KEY="<key>"
make setup
make run
```

`make run` opens the terminal UI. To start straight away:

```bash
make run REPO=https://github.com/owner/repo ISSUE=https://github.com/owner/repo/issues/123
```

`ISSUE` also takes plain text or `@path/to/issue.md`. `make headless` runs without the UI. `make test` runs the offline test suite; `make doctor` checks the key, provider and model.

The full write-up is in progress.
