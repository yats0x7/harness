# Wrench run: VERIFIED

**Issue:** Can't read back files that contain a double quote inside a quoted field  
**Model:** qwen/qwen3.8-27b:free via openrouter  
**Time:** 565s · **Requests:** 10 · **Tokens:** 57,834 in (39% cached) / 10,743 out

## What changed

In rowfile/lexer.py, split_fields mishandled doubled quotes ("" escape) inside quoted fields: it appended one literal quote but only advanced one character, so the second quote of the pair was then treated as the closing quote and the next character raised ParseError "unexpected character after closing quote". Fixed by advancing the index one extra character when a doubled quote is consumed inside quotes. This makes read_records round-trip values written by write_records, including values like 'The "best" one' and 'Hose, 1/2" x 50\''; existing behavior (quoted fields with delimiters, whitespace after closing quote, unterminated-quote and garbage-after-quote errors) is unchanged.

## Evidence

| Check | Original code | With the fix | Result |
|---|---|---|---|
| Reproduction `python ~/harness/runs/20260927-042241-py-csv-quotes/scratch/repro.py` | exit 1 | exit 0 | yes |
| Tests `python -m pytest -q -p no:cacheprovider` | not rerun (passed after) | 19 passed | no new failures |

Reviewer approved.

## Patch

```diff
diff --git a/rowfile/lexer.py b/rowfile/lexer.py
index 8e56527..80f5e19 100644
--- a/rowfile/lexer.py
+++ b/rowfile/lexer.py
@@ -23,6 +23,7 @@ def split_fields(line, dialect):
             if ch == quote:
                 if i + 1 < n and line[i + 1] == quote:
                     buf.append(quote)
+                    i += 1
                 else:
                     in_quotes = False
             else:
```

Files: `patch.diff`, `summary.json`, `trajectory.jsonl` (every step), `outputs/` (full tool output).