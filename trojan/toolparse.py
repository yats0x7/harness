"""Recovering tool calls that models write as plain text.

DeepSeek and Qwen both have a native tool-call syntax that sometimes leaks into
the message content instead of arriving as structured `tool_calls`, especially
at long context or behind a misconfigured server. The same parser also drives
the harness's text-mode fallback, which asks the model to write Qwen-style XML
tool calls directly. Every format below maps to (tool name, arguments dict).
"""
from __future__ import annotations

import ast
import json
import re
from typing import Any, Dict, List, Optional, Tuple

# ── Argument JSON repair ─────────────────────────────────────────────────────

_WRAPPER_KEYS = ("arguments", "input", "parameters", "args")


def _normalize(value: Any) -> Tuple[Dict[str, Any], Optional[str]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}, "arguments were a string, not a JSON object"
    if not isinstance(value, dict):
        return {}, "arguments must be a JSON object"
    extra = set(value) - {"name", "type"}
    if len(extra) == 1:
        key = next(iter(extra))
        if key in _WRAPPER_KEYS and isinstance(value[key], (dict, str)):
            return _normalize(value[key])
    return value, None


def _balance(s: str) -> str:
    """Close brackets and quotes left open by a truncated JSON string."""
    stack: List[str] = []
    in_str = False
    escaped = False
    for ch in s:
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    return s + ('"' if in_str else "") + "".join(reversed(stack))


def parse_arguments(raw: Any, unwrap: bool = True) -> Tuple[Dict[str, Any], Optional[str]]:
    """Parse a tool call's arguments, repairing common damage.

    Returns (arguments, error). On failure the arguments are empty and the error
    explains why, so the model can be told exactly what to fix. With `unwrap`,
    a stray {"arguments": {...}} wrapper around the real arguments is removed.
    """
    norm = _normalize if unwrap else (lambda v: (v, None) if isinstance(v, dict) else ({}, "arguments must be a JSON object"))
    if raw is None:
        return {}, None
    if isinstance(raw, dict):
        return norm(raw)
    s = str(raw).strip()
    if not s:
        return {}, None
    if s.startswith("```"):
        s = re.sub(r"^```[A-Za-z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    candidates = [s, re.sub(r",\s*([}\]])", r"\1", s), _balance(s)]
    first, last = s.find("{"), s.rfind("}")
    if 0 <= first < last:
        candidates.append(s[first:last + 1])
    for cand in candidates:
        try:
            return norm(json.loads(cand, strict=False))
        except ValueError:
            continue
    try:
        value = ast.literal_eval(s)
        if isinstance(value, dict):
            return norm(value)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        pass
    return {}, "could not parse the arguments as JSON: " + s[:200]


# ── Text tool-call formats ───────────────────────────────────────────────────

_BAR = "[|｜]"  # DeepSeek uses the full-width vertical bar in special tokens
_DSML_INVOKE = re.compile(
    rf"<{_BAR}DSML{_BAR}invoke\s+name=\"([^\"]+)\"\s*>(.*?)</{_BAR}DSML{_BAR}invoke>", re.S)
_DSML_PARAM = re.compile(
    rf"<{_BAR}DSML{_BAR}parameter\s+name=\"([^\"]+)\"([^>]*)>(.*?)</{_BAR}DSML{_BAR}parameter>", re.S)
_DS_LEGACY = re.compile(
    rf"<{_BAR}tool[▁_ ]call[▁_ ]begin{_BAR}>\s*(?:function)?\s*<{_BAR}tool[▁_ ]sep{_BAR}>\s*([\w.\-]+)\s*"
    rf"(?:```(?:json)?\s*(.*?)\s*```|(\{{.*?\}}))\s*<{_BAR}tool[▁_ ]call[▁_ ]end{_BAR}>", re.S)
_QWEN_FN = re.compile(r"<function=([\w.\-]+)>(.*?)(?:</function>|(?=<function=)|(?=</tool_call>)|\Z)", re.S)
_QWEN_PARAM = re.compile(r"<parameter=([\w.\-]+)>(.*?)(?:</parameter>|(?=<parameter=)|(?=</function>)|\Z)", re.S)
_JSON_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
_FENCE = re.compile(r"```.*?```", re.S)
_LEFTOVER = re.compile(
    rf"</?tool_call>|<{_BAR}DSML{_BAR}/?tool_calls>|</{_BAR}DSML{_BAR}tool_calls>|"
    rf"<{_BAR}tool[▁_ ]calls?[▁_ ](?:begin|end){_BAR}>")


def _mask_fences(text: str) -> str:
    """Blank out fenced code so tool syntax quoted inside code is ignored."""
    return _FENCE.sub(lambda m: " " * len(m.group(0)), text)


def _strip_one_newline(value: str) -> str:
    if value.startswith("\r\n"):
        value = value[2:]
    elif value.startswith("\n"):
        value = value[1:]
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    return value


def coerce(value: str, schema: Optional[dict]) -> Any:
    """Convert a textual parameter value to the type the tool schema expects."""
    kind = (schema or {}).get("type")
    v = value.strip()
    try:
        if kind == "integer":
            return int(float(v))
        if kind == "number":
            return float(v)
        if kind == "boolean":
            return v.lower() in ("true", "1", "yes", "y")
        if kind in ("array", "object"):
            return json.loads(v)
    except ValueError:
        return value
    return value


def extract_text_tool_calls(
    text: str, schemas: Optional[Dict[str, dict]] = None
) -> Tuple[List[Tuple[str, Dict[str, Any], Optional[str]]], str]:
    """Find tool calls written as text.

    `schemas` maps tool name to its JSON-schema `parameters`, used to coerce
    values. Returns ([(name, arguments, error)], text with the calls removed).
    """
    if not text:
        return [], text
    schemas = schemas or {}
    masked = _mask_fences(text)
    found: List[Tuple[int, int, str, Dict[str, Any], Optional[str]]] = []

    def props(name: str) -> dict:
        return (schemas.get(name) or {}).get("properties", {})

    for m in _DSML_INVOKE.finditer(masked):
        name = m.group(1)
        body = text[m.start(2):m.end(2)]
        args: Dict[str, Any] = {}
        for pname, attrs, value in _DSML_PARAM.findall(body):
            if 'string="false"' in attrs:
                try:
                    args[pname] = json.loads(value)
                    continue
                except ValueError:
                    pass
            args[pname] = coerce(value, props(name).get(pname)) if props(name).get(pname, {}).get("type") not in (None, "string") else value
        found.append((m.start(), m.end(), name, args, None))

    for m in _DS_LEGACY.finditer(text):  # its payload is itself fenced, so search the raw text
        name = m.group(1)
        raw = text[m.start(2):m.end(2)] if m.group(2) is not None else text[m.start(3):m.end(3)]
        args, err = parse_arguments(raw)
        found.append((m.start(), m.end(), name, args, err))

    for m in _QWEN_FN.finditer(masked):
        if any(s <= m.start() < e for s, e, *_ in found):
            continue
        name = m.group(1)
        body = text[m.start(2):m.end(2)]
        args = {}
        for pname, value in _QWEN_PARAM.findall(body):
            value = _strip_one_newline(value)
            schema = props(name).get(pname)
            args[pname] = coerce(value, schema) if schema and schema.get("type") != "string" else value
        found.append((m.start(), m.end(), name, args, None))

    for m in _JSON_CALL.finditer(masked):
        if any(s <= m.start() < e for s, e, *_ in found):
            continue
        obj, err = parse_arguments(text[m.start(1):m.end(1)], unwrap=False)
        name = str(obj.get("name", "")) if not err else ""
        if name:
            raw_args = obj.get("arguments", obj.get("parameters", {}))
            args, err = parse_arguments(raw_args)
            found.append((m.start(), m.end(), name, args, err))

    found.sort(key=lambda f: f[0])
    cleaned = text
    for start, end, *_ in sorted(found, key=lambda f: -f[0]):
        cleaned = cleaned[:start] + cleaned[end:]
    cleaned = _LEFTOVER.sub("", cleaned).strip()
    return [(name, args, err) for _, _, name, args, err in found], cleaned


_BASH_BLOCK = re.compile(r"```(?:bash|sh|shell|console)\s*\n(.*?)```", re.S)


def extract_bash_block(text: str) -> Optional[str]:
    """Last fenced shell block, for text mode when the model skips the XML."""
    blocks = _BASH_BLOCK.findall(text or "")
    return blocks[-1].strip() if blocks else None
