"""Enrichment pack error hierarchy."""

from __future__ import annotations


class PackError(Exception):
    """Base error for enrichment packs."""


class PackLoadError(PackError):
    """Pack path/archive could not be loaded safely."""


class PackValidationError(PackError):
    """Pack failed structural or semantic validation."""

    def __init__(self, message: str, *, errors: list[str] | None = None):
        super().__init__(message)
        self.errors = list(errors or ([message] if message else []))


class PackCompatibilityError(PackError):
    """Pack is incompatible with the running Redibis version or output contract."""


class ContextReductionRequired(PackError):
    """Context reduction is required and has not been approved yet."""

    def __init__(self, message: str, *, plan: dict | None = None):
        super().__init__(message)
        self.plan = plan or {}
