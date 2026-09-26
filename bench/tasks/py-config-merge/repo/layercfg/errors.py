class ConfigError(Exception):
    """A settings source could not be read or has the wrong shape."""


class MissingSetting(KeyError):
    """A dotted setting path does not exist."""

    def __str__(self):
        return "setting not found: %s" % self.args[0]
