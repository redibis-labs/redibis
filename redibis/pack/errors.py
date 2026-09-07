"""Error hierarchy for portable Redibis Packs (``.rdbpack``)."""

from __future__ import annotations


class RdbPackError(Exception):
    """Base error for Redibis Pack operations."""


class PackLoadError(RdbPackError):
    """Archive/path could not be loaded safely."""


class PackValidationError(RdbPackError):
    """Pack failed structural, checksum, or semantic validation."""

    def __init__(self, message: str, *, errors: list[str] | None = None):
        self.errors = list(errors or ([message] if message else []))
        detail = message
        if self.errors and self.errors[0] != message:
            detail = f"{message}: {self.errors[0]}"
        super().__init__(detail)


class PackCompatibilityError(RdbPackError):
    """Pack is incompatible with the running Redibis version or registries."""

    def __init__(self, message: str, *, errors: list[str] | None = None):
        self.errors = list(errors or ([message] if message else []))
        detail = message
        if self.errors and self.errors[0] != message:
            detail = f"{message}: {self.errors[0]}"
        super().__init__(detail)
