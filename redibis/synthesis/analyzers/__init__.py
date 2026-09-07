"""Analyzer package — import modules to register built-ins."""

from redibis.synthesis.analyzers import datastage as _datastage  # noqa: F401
from redibis.synthesis.analyzers import requirements as _requirements  # noqa: F401
from redibis.synthesis.analyzers import spark as _spark  # noqa: F401
from redibis.synthesis.analyzers import sql as _sql  # noqa: F401
from redibis.synthesis.analyzers.base import list_analyzers, run_analyzers

__all__ = ["list_analyzers", "run_analyzers"]
