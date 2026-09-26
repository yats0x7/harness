from wrench.config import load_config
from wrench.llm import LLMClient, pick_model, resolve_endpoint

from conftest import tool_reply


def test_pick_model_prefers_the_configured_order():
    assert pick_model(["deepseek-v4-pro", "deepseek-flash"], ["deepseek-flash", "deepseek-v4-pro"]) == "deepseek-v4-pro"


def test_pick_model_falls_back_to_a_coding_chat_model():
    available = ["text-embedding-v3", "qwen3-vl-plus", "qwen3-coder-next", "qwen-turbo"]
    assert pick_model(["not-served"], available) == "qwen3-coder-next"


def test_custom_endpoint_and_model_from_env(fake_model):
    cfg = load_config()
    ep = resolve_endpoint(cfg)
    assert ep.base_url == fake_model.url and ep.model == "deepseek-v4-pro"


def test_request_shape(fake_model):
    cfg = load_config()
    llm = LLMClient(resolve_endpoint(cfg), cfg.model)
    fake_model.script = [tool_reply(("bash", {"command": "ls"}))]
    reply = llm.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {
        "name": "bash", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}}])
    body = fake_model.requests[0]
    assert body["model"] == "deepseek-v4-pro" and body["temperature"] == 0.0 and body["seed"] == 42
    assert reply.tool_calls[0].arguments == {"command": "ls"}
    assert reply.usage.cached == 800
