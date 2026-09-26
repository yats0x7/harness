"""Keeping the prompt small without wrecking the provider's prompt cache.

Providers such as DeepSeek cache the longest unchanged prefix of the prompt, and
cache hits cost a small fraction of fresh input. Editing an early message breaks
the cache from that point onward, so this module only rewrites history when the
prompt is genuinely too large, and then does all its rewriting in one batch:
old tool outputs are masked (replaced with a short stub) and, if that is not
enough, the oldest turns are dropped and replaced by a note.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


def estimate_tokens(messages: List[Dict[str, Any]]) -> int:
    chars = 0
    for m in messages:
        chars += len(m.get("content") or "") + len(m.get("reasoning_content") or "")
        for tc in m.get("tool_calls") or []:
            chars += len(json.dumps(tc))
    return int(chars / 3.2) + 8 * len(messages)


class Conversation:
    def __init__(self, system: str, task: str, window: int, compact_at: float, keep_recent: int):
        self.messages: List[Dict[str, Any]] = [{"role": "system", "content": system},
                                               {"role": "user", "content": task}]
        self.window = window
        self.compact_at = compact_at
        self.keep_recent = keep_recent
        self.last_prompt_tokens = 0
        self._counted_upto = 0
        self.compactions = 0

    def add(self, message: Dict[str, Any]) -> None:
        self.messages.append(message)

    def note_usage(self, prompt_tokens: int) -> None:
        if prompt_tokens:
            self.last_prompt_tokens = prompt_tokens
            self._counted_upto = len(self.messages)

    def size(self) -> int:
        if self.last_prompt_tokens and self._counted_upto <= len(self.messages):
            return self.last_prompt_tokens + estimate_tokens(self.messages[self._counted_upto:])
        return estimate_tokens(self.messages)

    def _boundary(self, index: int) -> int:
        """Move an index forward to the start of a turn (an assistant message)."""
        while index < len(self.messages) and self.messages[index]["role"] != "assistant":
            index += 1
        return index

    def maybe_compact(self, force: bool = False, state_note: str = "") -> Optional[str]:
        limit = int(self.window * self.compact_at)
        if not force and self.size() <= limit:
            return None
        cutoff = self._boundary(max(2, len(self.messages) - self.keep_recent))
        masked = 0
        for m in self.messages[2:cutoff]:
            content = m.get("content") or ""
            if m["role"] in ("tool", "user") and len(content) > 400 and not m.get("_pinned"):
                first = content.strip().splitlines()[0][:160] if content.strip() else ""
                m["content"] = f"[old tool output elided to save context: {first} ...]"
                masked += 1
            if m["role"] == "assistant" and len(content) > 1500:
                m["content"] = content[:600] + "\n[... elided ...]"
        self.last_prompt_tokens = 0
        report = f"masked {masked} old tool outputs"
        if self.size() > limit or force:
            start = 2
            while start < cutoff and self.messages[start].get("_pinned"):
                start += 1
            end = self._boundary(start + max(2, (cutoff - start) // 2))
            if end > start:
                dropped = end - start
                note = {"role": "user", "_pinned": True,
                        "content": "[The harness removed " + str(dropped) + " older messages to stay within the "
                                   "context budget. Current state:\n" + state_note + "]"}
                self.messages[start:end] = [note]
                report += f", dropped {dropped} older messages"
        self.compactions += 1
        return report
