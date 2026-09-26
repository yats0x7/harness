class InvalidPage(Exception):
    """Raised when a requested page number cannot be served."""


class PageNotAnInteger(InvalidPage):
    """The page number could not be interpreted as an integer."""


class EmptyPage(InvalidPage):
    """The page number is outside the range of available pages."""
