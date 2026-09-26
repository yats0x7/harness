"""Shared fixtures: a scripted fake OpenAI-compatible server and throwaway repos."""
from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Union

import pytest

Script = List[Union[Dict[str, Any], Callable[[Dict[str, Any]], Dict[str, Any]]]]


def tool_reply(*calls, content: str = "", reasoning: str = "") -> Dict[str, Any]:
    """A chat completion whose message calls the given (name, args) tools."""
    msg: Dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = [{"id": f"call_{i}_{name}", "type": "function",
                              "function": {"name": name, "arguments": json.dumps(args)}}
                             for i, (name, args) in enumerate(calls)]
    if reasoning:
        msg["reasoning_content"] = reasoning
    return {"choices": [{"message": msg, "finish_reason": "tool_calls" if calls else "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 50, "prompt_cache_hit_tokens": 800}}


def text_reply(content: str) -> Dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 20}}


class FakeModel:
    def __init__(self):
        self.script: Script = []
        self.requests: List[Dict[str, Any]] = []
        self.errors: List[int] = []  # HTTP statuses to return before the next scripted reply
        self.models = ["deepseek-v4-pro", "deepseek-flash"]
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.endswith("/models"):
                    self._send(200, {"data": [{"id": m} for m in fake.models]})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                if fake.errors:
                    self._send(fake.errors.pop(0), {"error": {"message": "scripted error"}})
                    return
                if not fake.script:
                    self._send(200, tool_reply(("finish", {"summary": "script exhausted"})))
                    return
                item = fake.script.pop(0)
                reply = item(body) if callable(item) else item
                if body.get("stream"):
                    self._send_stream(reply)
                else:
                    self._send(200, reply)

            def _send_stream(self, reply):
                """Replay a completion as SSE chunks, splitting text and arguments across chunks."""
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                msg = reply["choices"][0]["message"]
                chunks = [{"role": "assistant"}]
                r = msg.get("reasoning_content") or ""
                if r:
                    chunks += [{"reasoning_content": r[: len(r) // 2]}, {"reasoning_content": r[len(r) // 2:]}]
                c = msg.get("content") or ""
                if c:
                    chunks += [{"content": c[: len(c) // 2]}, {"content": c[len(c) // 2:]}]
                for i, tc in enumerate(msg.get("tool_calls") or []):
                    args = tc["function"]["arguments"]
                    chunks.append({"tool_calls": [{"index": i, "id": tc["id"], "type": "function",
                                                   "function": {"name": tc["function"]["name"], "arguments": args[:5]}}]})
                    chunks.append({"tool_calls": [{"index": i, "function": {"arguments": args[5:]}}]})
                events = [{"choices": [{"index": 0, "delta": d}]} for d in chunks]
                events.append({"choices": [{"index": 0, "delta": {}, "finish_reason": reply["choices"][0].get("finish_reason")}]})
                events.append({"choices": [], "usage": reply.get("usage", {})})
                for e in events:
                    self.wfile.write(b"data: " + json.dumps(e).encode() + b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        return H

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake_model(monkeypatch):
    fm = FakeModel()
    monkeypatch.setenv("AI_BASE_URL", fm.url)
    monkeypatch.setenv("AI_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("AI_API_KEY", "test-key-not-real")
    yield fm
    fm.close()


def make_repo(root: Path, files: Dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"], cwd=root, check=True)
    return root


BUGGY = {
    "calc/__init__.py": "from .ops import mean\n",
    "calc/ops.py": "def mean(values):\n    return sum(values) / (len(values) + 1)\n",
    "tests/test_ops.py": "from calc import mean\n\n\ndef test_single():\n    assert mean([0]) == 0\n",
    "pyproject.toml": "[project]\nname = \"calc\"\nversion = \"0.1\"\n",
}


@pytest.fixture
def buggy_repo(tmp_path):
    return make_repo(tmp_path / "repo", BUGGY)
