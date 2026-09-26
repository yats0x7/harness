"""Built-in defaults. Every key a service reads should have a default here."""

DEFAULTS = {
    "app": {
        "name": "service",
        "debug": False,
        "log_level": "INFO",
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8080,
        "workers": 2,
        "tls": {
            "enabled": False,
            "cert_file": None,
            "key_file": None,
            "min_version": "TLSv1.2",
        },
    },
    "database": {
        "url": "sqlite:///app.db",
        "echo": False,
        "pool": {
            "min_size": 1,
            "max_size": 10,
            "timeout": 30.0,
            "recycle": 1800,
        },
    },
    "cache": {
        "backend": "memory",
        "ttl": 300,
        "redis": {
            "url": "redis://localhost:6379/0",
            "retry": {"attempts": 3, "backoff": 0.5},
        },
    },
    "features": {},
}
