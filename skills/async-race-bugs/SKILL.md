---
name: async-race-bugs
description: Bugs that depend on timing or interleaving (queues, drain/flush callbacks, concurrent enqueue, promises, locks, events). Use when the issue mentions concurrency, "while processing", callbacks not firing, or ordering.
---
# Timing and concurrency bugs

## Reproduce deterministically
Never rely on sleeps. Control the interleaving with a gate: a promise you resolve by hand.

```ts
let release!: () => void;
const gate = new Promise<void>((r) => (release = r));
const q = new Queue({ process: async () => { await gate; } });
q.enqueue("a");          // starts processing, blocks on the gate
q.enqueue("b"); q.enqueue("c");   // fill the queue while the first item is in flight
release();               // let it drain
await new Promise((r) => setTimeout(r, 0));  // flush microtasks, then assert
```
In Python use `asyncio.Event` the same way.

## Common root causes
- A flag computed once before a loop, when the condition can change during the loop (re-check it
  inside the loop, or record it at the moment it happens).
- State reset in `finally` before the callback that depends on it runs, or the reverse.
- A callback that should fire once per episode firing zero times or on every cycle: track the episode
  with an explicit flag that is set when the condition occurs and cleared after the callback.

## Verify
- Write the gated reproduction as a regression test that fails on the original code.
- Test the edge cases: capacity 1, errors thrown by the worker, `clear()` during processing, and two
  full episodes in a row (the callback must fire again the second time).
