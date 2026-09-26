# Setting one pool option in settings.json makes the other pool defaults disappear

After upgrading to layercfg 1.2.0 our worker started crashing on boot with `MissingSetting: setting not found: database.pool.timeout`. We only changed the pool size.

### settings.json

```json
{
  "database": {
    "pool": { "max_size": 50 }
  }
}
```

### Reproduce

```python
from layercfg import load_settings

s = load_settings("settings.json", env={})
print(s.get("database.pool.max_size"))
print(s.get("database.pool.timeout"))
```

### Actual

```
50
Traceback (most recent call last):
  ...
layercfg.errors.MissingSetting: setting not found: database.pool.timeout
```

`s.section("database.pool").as_dict()` is just `{"max_size": 50}`; `min_size`, `timeout` and `recycle` are all gone.

### Expected

```
50
30.0
```

Only the key I set should change. Everything else in `database.pool` should keep its built-in default, the same way that setting `server.port` alone keeps `server.host`.

The same thing happens with environment variables, e.g. `APP__SERVER__TLS__ENABLED=true` loses `server.tls.min_version`, and `APP__CACHE__REDIS__URL=...` loses the `cache.redis.retry` block. Overriding a key directly under a top-level section (like `server.port`) works fine.
