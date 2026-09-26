"""Layered settings loader."""
from .errors import ConfigError, MissingSetting
from .settings import Settings, load_settings

__all__ = ["ConfigError", "MissingSetting", "Settings", "load_settings"]
__version__ = "1.2.0"
