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


def test_qwen_gets_its_recommended_sampling(fake_model, monkeypatch):
    monkeypatch.setenv("AI_MODEL", "qwen3-coder-next")
    cfg = load_config()
    llm = LLMClient(resolve_endpoint(cfg), cfg.model)
    fake_model.script = [tool_reply(("bash", {"command": "ls"}))]
    llm.chat([{"role": "user", "content": "hi"}])
    body = fake_model.requests[0]
    assert body["temperature"] == 0.7 and body["top_p"] == 0.8 and body["seed"] == 42
    assert llm.echo_reasoning is False


def test_non_streaming_mode_still_works(fake_model):
    cfg = load_config()
    cfg.model.stream = False
    llm = LLMClient(resolve_endpoint(cfg), cfg.model)
    fake_model.script = [tool_reply(("bash", {"command": "ls"}), reasoning="r")]
    reply = llm.chat([{"role": "user", "content": "hi"}])
    assert "stream" not in fake_model.requests[0] or fake_model.requests[0]["stream"] is False
    assert reply.tool_calls[0].arguments == {"command": "ls"} and reply.reasoning == "r"


def test_streamed_reply_is_reassembled(fake_model):
    cfg = load_config()
    llm = LLMClient(resolve_endpoint(cfg), cfg.model)
    fake_model.script = [tool_reply(("edit_file", {"path": "a.py", "old_str": "x = 1", "new_str": "x = 2"}),
                                    content="fixing", reasoning="the value is wrong")]
    reply = llm.chat([{"role": "user", "content": "hi"}])
    assert fake_model.requests[0]["stream"] is True
    assert reply.content == "fixing" and reply.reasoning == "the value is wrong"
    assert reply.tool_calls[0].name == "edit_file"
    assert reply.tool_calls[0].arguments == {"path": "a.py", "old_str": "x = 1", "new_str": "x = 2"}
    assert reply.usage.prompt == 1000 and reply.usage.cached == 800


def test_bedrock_keys_are_recognised_and_output_is_capped(monkeypatch):
    from wrench.config import load_config
    cfg = load_config()
    bedrock = [p for p in cfg.providers.values() if p.key_prefix == "ABSK"]
    assert bedrock and all("bedrock-mantle" in p.base_url and p.max_output_tokens == 8192 for p in bedrock)
    from wrench.llm import Endpoint, LLMClient
    llm = LLMClient(Endpoint("bedrock-us-east-1", bedrock[0].base_url, "deepseek.v3.2", "x", [], 8192), cfg.model)
    assert llm._max_tokens(None) == 8192 and llm.family == "deepseek"
