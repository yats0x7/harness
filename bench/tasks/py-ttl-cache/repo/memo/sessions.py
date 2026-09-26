"""Server-side session storage with an idle timeout."""
import secrets

from .cache import TTLCache


class SessionStore:
    """Sessions live for ``idle_timeout`` seconds after they were last saved.

    Handlers call ``save`` at the end of each request, which keeps an active
    session alive. When ``max_sessions`` is reached the least recently used
    session is dropped.
    """

    def __init__(self, idle_timeout=1800, max_sessions=10000, clock=None):
        self._cache = TTLCache(maxsize=max_sessions, ttl=idle_timeout, clock=clock)

    def create(self, data=None):
        sid = secrets.token_urlsafe(24)
        self._cache.set(sid, dict(data or {}))
        return sid

    def load(self, sid):
        """Return the session dict, or ``None`` if it expired or never existed."""
        data = self._cache.get(sid)
        return dict(data) if data is not None else None

    def save(self, sid, data):
        self._cache.set(sid, dict(data))

    def destroy(self, sid):
        return self._cache.delete(sid)

    def expires_in(self, sid):
        return self._cache.ttl_remaining(sid)

    def __len__(self):
        return len(self._cache)
