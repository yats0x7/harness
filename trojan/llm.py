"""Chat-completions client for any OpenAI-compatible endpoint.

DeepSeek, Qwen (DashScope), OpenRouter, SiliconFlow, Together and Ollama all
speak the same `/chat/completions` protocol with small differences. This module
hides those differences: provider detection, retries, thinking-mode settings,
echoing reasoning back to DeepSeek, and token accounting.
"""
from __future__ import annotations

import json
import random
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from .config import Config, ModelSettings, Provider
from .toolparse import parse_arguments


class LLMError(Exception):
    """A model call failed in a way retrying will not fix."""


class AuthError(LLMError):
    pass


class ContextOverflow(LLMError):
    pass


class ToolsUnsupported(LLMError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]
    error: Optional[str] = None


@dataclass
class Usage:
    prompt: int = 0
    completion: int = 0
    cached: int = 0
    requests: int = 0

    def add(self, other: "Usage") -> None:
        self.prompt += other.prompt
        self.completion += other.completion
        self.cached += other.cached
        self.requests += other.requests

    @property
    def total(self) -> int:
        return self.prompt + self.completion


@dataclass
class Reply:
    content: str
    reasoning: str
    tool_calls: List[ToolCall]
    finish_reason: str
    usage: Usage
    message: Dict[str, Any]  # the assistant message to append to history


@dataclass
class Endpoint:
    provider: str
    base_url: str
    model: str
    api_key: str
    available: List[str] = field(default_factory=list)
    max_output: int = 0  # provider cap on max_tokens; 0 means none


_OVERFLOW = re.compile(
    r"context[ _-]?length|maximum context|too many tokens|context window|prompt is too long|"
    r"input (?:is )?too long|exceeds? the (?:model'?s? )?(?:maximum|max)|reduce the length", re.I)
_THINK = re.compile(r"<think>(.*?)</think>", re.S)


def _headers(key: Optional[str]) -> Dict[str, str]:
    headers = {"Content-Type": "application/json", "User-Agent": "trojan-horse/0.1"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


class LLMClient:
    def __init__(self, endpoint: Endpoint, settings: ModelSettings):
        self.endpoint = endpoint
        self.settings = settings
        self.usage = Usage()
        where = (endpoint.base_url + " " + endpoint.model).lower()
        self.family = "deepseek" if "deepseek" in where else "qwen" if "qwen" in where else "other"
        # DeepSeek's thinking mode requires the reasoning to be sent back inside
        # a tool loop; other providers ignore or reject the field.
        self.echo_reasoning = self.family == "deepseek"
        self._minimal = False
        self.stream = settings.stream
        # Called while a streamed reply arrives: progress(reasoning_chars, content_chars).
        self.progress: Optional[Callable[[int, int], None]] = None
        # With streaming, the read timeout is the longest silence allowed between
        # chunks, so a model that thinks for ten minutes is never cut off.
        self._http = httpx.Client(timeout=httpx.Timeout(settings.request_timeout, connect=20))

    # ── request building ──────────────────────────────────────────────────
    def _thinking_params(self) -> Dict[str, Any]:
        mode = self.settings.thinking
        if mode == "default":
            return {}
        on = mode == "on"
        url = self.endpoint.base_url
        if "deepseek.com" in url:
            return {"thinking": {"type": "enabled" if on else "disabled"}}
        if "dashscope" in url or "aliyuncs" in url:
            return {"enable_thinking": on}
        if "openrouter" in url:
            return {"reasoning": {"enabled": on}}
        if "localhost:11434" in url or "127.0.0.1:11434" in url:  # Ollama
            return {} if on else {"reasoning_effort": "none"}
        return {}

    def _clean(self, message: Dict[str, Any]) -> Dict[str, Any]:
        out = {k: v for k, v in message.items() if not k.startswith("_")}
        if out.get("role") == "assistant":
            if not self.echo_reasoning:
                out.pop("reasoning_content", None)
            if out.get("content") is None:
                out["content"] = ""
        return out

    def _max_tokens(self, requested: Optional[int]) -> int:
        value = requested or self.settings.max_output_tokens
        cap = self.endpoint.max_output or (8192 if "bedrock" in self.endpoint.base_url else 0)
        return min(value, cap) if cap else value

    def _body(self, messages, tools, max_tokens) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "model": self.endpoint.model,
            "messages": [self._clean(m) for m in messages],
            "max_tokens": self._max_tokens(max_tokens),
            "stream": False,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if self._minimal:
            body["max_tokens"] = min(body["max_tokens"], 8192)
            return body
        body.update(self._thinking_params())
        family = self.settings.sampling.get(self.family, {})
        temperature = family.get("temperature", self.settings.temperature)
        if temperature is not None and self.settings.thinking != "on":
            body["temperature"] = temperature
        if "top_p" in family:
            body["top_p"] = family["top_p"]
        if self.settings.seed is not None:
            body["seed"] = self.settings.seed
        return body

    # ── the call ──────────────────────────────────────────────────────────
    def chat(self, messages: List[Dict[str, Any]], tools: Optional[list] = None,
             max_tokens: Optional[int] = None) -> Reply:
        url = self.endpoint.base_url + "/chat/completions"
        last_error = "no attempt made"
        reasoning_flipped = False
        for attempt in range(self.settings.max_retries + 1):
            body = self._body(messages, tools, max_tokens)
            try:
                if self.stream:
                    status, data, text, headers = self._post_stream(url, body)
                else:
                    resp = self._http.post(url, json=body, headers=_headers(self.endpoint.api_key))
                    status, text, headers = resp.status_code, resp.text[:2000], resp.headers
                    data = None
                    if status == 200:
                        try:
                            data = resp.json()
                        except ValueError:
                            data = {}
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                self._sleep(attempt)
                continue

            if status == 200:
                if data and data.get("choices"):
                    return self._parse(data)
                last_error = f"no choices in response: {json.dumps(data)[:300] if data else '(empty)'}"
                self._sleep(attempt)
                continue

            lowered = text.lower()
            if self.stream and status in (400, 422) and "stream" in lowered:
                self.stream = False  # this endpoint cannot stream; use plain requests
                continue
            if status in (401, 403):
                raise AuthError(f"HTTP {status}: the endpoint rejected the API key. {text[:300]}")
            if status in (400, 413, 422):
                if _OVERFLOW.search(text):
                    raise ContextOverflow(text[:300])
                if "reasoning_content" in lowered and not reasoning_flipped:
                    self.echo_reasoning = not self.echo_reasoning
                    reasoning_flipped = True
                    continue
                if tools and re.search(r"\btools?\b|function", lowered) and not self._minimal:
                    self._minimal = True  # first try without optional params
                    continue
                if tools and re.search(r"\btools?\b|function", lowered):
                    raise ToolsUnsupported(text[:300])
                if not self._minimal:
                    self._minimal = True
                    continue
                raise LLMError(f"HTTP {status}: {text[:500]}")
            if status == 404:
                raise LLMError(f"HTTP 404 from {url}: {text[:300]} (is the model id right?)")
            last_error = f"HTTP {status}: {text[:300]}"
            retry_after = headers.get("retry-after")
            self._sleep(attempt, float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else None)
        raise LLMError(f"gave up after {self.settings.max_retries + 1} attempts; last error: {last_error}")

    def _post_stream(self, url: str, body: Dict[str, Any]):
        """POST with stream=true and rebuild a normal completion from the SSE chunks."""
        body = dict(body, stream=True, stream_options={"include_usage": True})
        timeout = httpx.Timeout(connect=20, read=self.settings.stream_idle_timeout, write=60, pool=60)
        with self._http.stream("POST", url, json=body, headers=_headers(self.endpoint.api_key),
                               timeout=timeout) as resp:
            if resp.status_code != 200:
                resp.read()
                return resp.status_code, None, resp.text[:2000], resp.headers
            content: List[str] = []
            reasoning: List[str] = []
            calls: Dict[int, Dict[str, str]] = {}
            finish = ""
            usage: Dict[str, Any] = {}
            n_reason = n_content = 0
            last_tick = time.time()
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                if chunk.get("error"):
                    return 500, None, json.dumps(chunk["error"])[:2000], {}
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for ch in chunk.get("choices") or []:
                    delta = ch.get("delta") or ch.get("message") or {}
                    if delta.get("content"):
                        content.append(delta["content"])
                        n_content += len(delta["content"])
                    r = delta.get("reasoning_content") or delta.get("reasoning")
                    if isinstance(r, str) and r:
                        reasoning.append(r)
                        n_reason += len(r)
                    for pos, tc in enumerate(delta.get("tool_calls") or []):
                        idx = tc.get("index", pos)
                        slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        name = fn.get("name") or ""
                        if name and name != slot["name"]:
                            slot["name"] = slot["name"] + name if slot["name"] and not name.startswith(slot["name"]) else name
                        args = fn.get("arguments")
                        if isinstance(args, dict):
                            slot["arguments"] = json.dumps(args)
                        elif args:
                            slot["arguments"] += args
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
                if self.progress and time.time() - last_tick > 1.0:
                    last_tick = time.time()
                    try:
                        self.progress(n_reason, n_content)
                    except Exception:
                        pass
        message: Dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if calls:
            message["tool_calls"] = [{"id": c["id"], "type": "function",
                                      "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                                     for _, c in sorted(calls.items())]
        return 200, {"choices": [{"message": message, "finish_reason": finish}], "usage": usage}, "", {}

    def _sleep(self, attempt: int, hint: Optional[float] = None) -> None:
        delay = hint if hint is not None else min(60.0, 2.0 * (2 ** attempt))
        time.sleep(delay * (0.75 + random.random() * 0.5))

    def _parse(self, data: Dict[str, Any]) -> Reply:
        choice = data["choices"][0]
        msg = choice.get("message") or {}
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        if "<think>" in content or "</think>" in content:
            inner = "\n".join(_THINK.findall(content))
            content = _THINK.sub("", content)
            if "</think>" in content:  # opening tag was in the prompt template
                head, _, content = content.partition("</think>")
                inner = head + inner
            reasoning = (reasoning + "\n" + inner).strip()
        content = content.strip()

        calls: List[ToolCall] = []
        wire_calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args, err = parse_arguments(fn.get("arguments"))
            call = ToolCall(id=tc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                            name=(fn.get("name") or "").strip(), arguments=args, error=err)
            calls.append(call)
            wire_calls.append({"id": call.id, "type": "function",
                               "function": {"name": call.name, "arguments": json.dumps(args)}})

        message: Dict[str, Any] = {"role": "assistant", "content": content}
        if wire_calls:
            message["tool_calls"] = wire_calls
        if reasoning:
            message["reasoning_content"] = reasoning

        u = data.get("usage") or {}
        details = u.get("prompt_tokens_details") or {}
        usage = Usage(prompt=int(u.get("prompt_tokens") or 0),
                      completion=int(u.get("completion_tokens") or 0),
                      cached=int(u.get("prompt_cache_hit_tokens") or details.get("cached_tokens") or 0),
                      requests=1)
        self.usage.add(usage)
        return Reply(content=content, reasoning=reasoning, tool_calls=calls,
                     finish_reason=choice.get("finish_reason") or "", usage=usage, message=message)


# ── endpoint discovery ───────────────────────────────────────────────────────

_NOT_CHAT = ("embed", "-vl", "vl-", "vision", "audio", "tts", "asr", "image", "ocr", "rerank",
             "omni", "guard", "moderation", "realtime", "speech", "wan", "flux")


def pick_model(preferences: List[str], available: List[str]) -> str:
    if not available:
        return preferences[0] if preferences else ""
    lookup = {a.lower(): a for a in available}
    for pref in preferences:
        if pref.lower() in lookup:
            return lookup[pref.lower()]

    def score(model_id: str) -> int:
        m = model_id.lower()
        if any(bad in m for bad in _NOT_CHAT):
            return -100
        s = 0
        if "deepseek" in m or "qwen" in m:
            s += 10
        if "coder" in m:
            s += 5
        if any(v in m for v in ("v4", "3.8", "3.7", "3.6", "3.5")):
            s += 3
        if "pro" in m or "max" in m:
            s += 2
        return s

    best = max(available, key=score)
    if score(best) > 0:
        return best
    return preferences[0] if preferences else available[0]


def list_models(base_url: str, key: Optional[str], timeout: float = 10) -> Tuple[int, List[str]]:
    try:
        resp = httpx.get(base_url + "/models", headers=_headers(key), timeout=timeout)
    except httpx.HTTPError:
        return 0, []
    if resp.status_code != 200:
        return resp.status_code, []
    try:
        data = resp.json()
    except ValueError:
        return resp.status_code, []
    items = data.get("data") if isinstance(data, dict) else data
    if isinstance(data, dict) and "models" in data and not items:
        items = data["models"]
    ids = []
    for item in items or []:
        if isinstance(item, dict):
            ids.append(str(item.get("id") or item.get("name") or ""))
        elif isinstance(item, str):
            ids.append(item)
    return 200, [i for i in ids if i]


def _probe(provider: Provider, key: str) -> Tuple[bool, List[str], str]:
    """Does this provider accept the key? Returns (ok, model ids, detail)."""
    if provider.auth_check:
        try:
            resp = httpx.get(provider.base_url + provider.auth_check, headers=_headers(key), timeout=10)
        except httpx.HTTPError as exc:
            return False, [], type(exc).__name__
        if resp.status_code != 200:
            return False, [], f"HTTP {resp.status_code}"
        _, models = list_models(provider.base_url, key)
        try:
            free_tier = bool((resp.json().get("data") or {}).get("is_free_tier"))
        except ValueError:
            free_tier = False
        if free_tier:  # an account without credits can only call the free variants
            models = [m for m in models if m.endswith(":free")]
            return True, models, "ok (free tier)"
        return True, models, "ok"
    status, models = list_models(provider.base_url, key)
    if status == 200:
        return True, models, "ok"
    if status in (401, 403) or status == 0:
        return False, [], f"HTTP {status}" if status else "unreachable"
    # Some endpoints do not serve /models at all. A one-token chat call settles it.
    if not provider.models:
        return False, [], f"HTTP {status}"
    try:
        resp = httpx.post(provider.base_url + "/chat/completions", headers=_headers(key), timeout=20,
                          json={"model": provider.models[0], "max_tokens": 1,
                                "messages": [{"role": "user", "content": "ping"}]})
    except httpx.HTTPError as exc:
        return False, [], type(exc).__name__
    if resp.status_code == 200:
        return True, [], "ok (chat probe)"
    return False, [], f"HTTP {resp.status_code}"


def resolve_endpoint(cfg: Config) -> Endpoint:
    """Work out which endpoint and model to use for this run."""
    key = cfg.api_key

    if cfg.env_base_url:
        _, models = list_models(cfg.env_base_url, key or None)
        model = cfg.env_model or pick_model([], models)
        if not model:
            raise LLMError("AI_BASE_URL is set but the endpoint lists no models; set AI_MODEL too.")
        return Endpoint("custom", cfg.env_base_url, model, key, models)

    pinned = cfg.env_provider or (cfg.model.provider if cfg.model.provider != "auto" else "")
    if pinned:
        if pinned not in cfg.providers:
            raise LLMError(f"unknown provider '{pinned}'; known: {', '.join(cfg.providers)}")
        prov = cfg.providers[pinned]
        if prov.requires_key and not key:
            raise AuthError("AI_API_KEY is not set. Run: export AI_API_KEY=\"<your key>\"  (or put AI_API_KEY=... in .env)")
        status, models = list_models(prov.base_url, key if prov.requires_key else None)
        if status == 0 and not prov.requires_key:
            raise LLMError(f"cannot reach {prov.base_url}; is the local server running?")
        model = cfg.env_model or pick_model(prov.models, models)
        return Endpoint(prov.name, prov.base_url.rstrip("/"), model, key, models, prov.max_output_tokens)

    if not key:
        raise AuthError("AI_API_KEY is not set. Run: export AI_API_KEY=\"<your key>\"  (or put AI_API_KEY=... in .env)")

    candidates = [p for p in cfg.providers.values() if p.auto]
    prefixed = [p for p in candidates if p.key_prefix and key.startswith(p.key_prefix)]
    ordered = prefixed + [p for p in candidates if p not in prefixed and not p.key_prefix] or candidates
    with ThreadPoolExecutor(max_workers=len(ordered)) as pool:
        results = list(pool.map(lambda p: _probe(p, key), ordered))
    for prov, (ok, models, _) in zip(ordered, results):
        if ok:
            model = cfg.env_model or pick_model(prov.models, models)
            return Endpoint(prov.name, prov.base_url.rstrip("/"), model, key, models, prov.max_output_tokens)
    detail = ", ".join(f"{p.name}: {r[2]}" for p, r in zip(ordered, results))
    raise AuthError("no known provider accepted AI_API_KEY (" + detail + "). "
                    "If the key is for another OpenAI-compatible endpoint, set AI_BASE_URL and AI_MODEL.")
