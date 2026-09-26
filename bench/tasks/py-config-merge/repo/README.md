# layercfg

Settings are resolved in three layers, later layers winning:

1. built-in defaults (`layercfg/defaults.py`)
2. a JSON settings file
3. environment variables of the form `APP__SECTION__KEY=value`

```python
from layercfg import load_settings

settings = load_settings("settings.json", env={"APP__SERVER__PORT": "9000"})
settings.get("server.port")             # 9000
settings.get("database.pool.timeout")   # 30.0
```

Run the tests with `python -m pytest -q tests`.
