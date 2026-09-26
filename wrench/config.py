"""Configuration loading.

Everything the harness needs to know about models and limits lives in
config/harness.toml. The API key is the one thing that never does: it comes
from the AI_API_KEY environment variable at runtime.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on 3.9/3.10
    import tomli as tomllib  # type: ignore

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "harness.toml"


@dataclass
class Provider:
    name: str
    base_url: str
    models: List[str] = field(default_factory=list)
    key_prefix: str = ""
    auth_check: str = ""
    requires_key: bool = True
    auto: bool = True


@dataclass
class ModelSettings:
    provider: str = "auto"
    temperature: Optional[float] = 0.0
    seed: Optional[int] = 42
    max_output_tokens: int = 32768
    request_timeout: float = 300
    max_retries: int = 6
    thinking: str = "default"
    sampling: Dict[str, Dict[str, float]] = field(default_factory=dict)


@dataclass
class AgentSettings:
    max_steps: int = 70
    max_total_tokens: int = 4_000_000
    context_window: int = 90_000
    compact_at: float = 0.8
    keep_recent_messages: int = 10
    tool_output_chars: int = 9000
    command_timeout: int = 300
    test_timeout: int = 900
    tool_mode: str = "auto"
    review: bool = True
    max_review_rounds: int = 1
    max_attempts: int = 2


@dataclass
class Config:
    model: ModelSettings
    agent: AgentSettings
    providers: Dict[str, Provider]
    pricing: Dict[str, Dict[str, float]]
    path: Path
    sampling: Dict[str, Dict[str, float]] = field(default_factory=dict)

    # Runtime overrides from the environment. None of these are secrets.
    env_provider: str = ""
    env_base_url: str = ""
    env_model: str = ""

    @property
    def api_key(self) -> str:
        return os.environ.get("AI_API_KEY", "").strip()

    def price_for(self, model: str) -> Optional[Dict[str, float]]:
        lowered = model.lower()
        for key, price in self.pricing.items():
            if key.lower() in lowered:
                return price
        return None


def _fill(obj, data: dict):
    for key, value in data.items():
        if hasattr(obj, key):
            setattr(obj, key, value)
    return obj


def load_config(path: Optional[str] = None) -> Config:
    cfg_path = Path(path or os.environ.get("WRENCH_CONFIG") or DEFAULT_CONFIG)
    with open(cfg_path, "rb") as fh:
        raw = tomllib.load(fh)

    model = _fill(ModelSettings(), raw.get("model", {}))
    agent = _fill(AgentSettings(), raw.get("agent", {}))
    providers = {}
    for name, spec in raw.get("providers", {}).items():
        providers[name] = _fill(Provider(name=name, base_url=spec["base_url"]), spec)

    env_thinking = os.environ.get("AI_THINKING", "").strip().lower()
    if env_thinking in ("on", "off", "default"):
        model.thinking = env_thinking

    model.sampling = raw.get("sampling", {})
    return Config(
        model=model,
        agent=agent,
        providers=providers,
        pricing=raw.get("pricing", {}),
        path=cfg_path,
        sampling=raw.get("sampling", {}),
        env_provider=os.environ.get("AI_PROVIDER", "").strip(),
        env_base_url=os.environ.get("AI_BASE_URL", "").strip().rstrip("/"),
        env_model=os.environ.get("AI_MODEL", "").strip(),
    )
