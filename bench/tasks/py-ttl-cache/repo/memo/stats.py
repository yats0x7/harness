from dataclasses import dataclass, asdict


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    expirations: int = 0

    @property
    def lookups(self):
        return self.hits + self.misses

    @property
    def hit_rate(self):
        return self.hits / self.lookups if self.lookups else 0.0

    def reset(self):
        self.hits = self.misses = self.evictions = self.expirations = 0

    def as_dict(self):
        data = asdict(self)
        data["hit_rate"] = round(self.hit_rate, 4)
        return data
