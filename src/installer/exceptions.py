"""Errors raised from this package."""


class InstallerError(Exception):
    """All exceptions raised from this package's code."""


class InvalidWheelSource(InstallerError):
    """When a wheel source violates a contract, or is not supported."""


class UnsafePathError(InstallerError, ValueError):
    """Raised when an installation path is rejected by strict path handling.

    Inherits from :py:class:`ValueError` as well, to make it easy for callers
    that used to rely on the path validation errors raised by the default
    destination to catch this too.
    """
