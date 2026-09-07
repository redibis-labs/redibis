"""Context variables stamped onto every log record."""

from __future__ import annotations

import contextvars
import logging
from contextlib import contextmanager
from typing import Iterator

run_id_var = contextvars.ContextVar("redibis_run_id", default="")
table_var = contextvars.ContextVar("redibis_table", default="")
column_var = contextvars.ContextVar("redibis_column", default="")
fn_var = contextvars.ContextVar("redibis_fn", default="")

_VARS = {
    "run_id": run_id_var,
    "table": table_var,
    "column": column_var,
    "fn": fn_var,
}


class ContextInjectFilter(logging.Filter):
    """Attach scan context fields to each ``LogRecord``."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = run_id_var.get()
        record.table = table_var.get()
        record.column = column_var.get()
        record.fn = fn_var.get()
        return True


@contextmanager
def bind_context(**kw: str) -> Iterator[None]:
    tokens: dict[contextvars.ContextVar[str], contextvars.Token[str]] = {}
    for key, value in kw.items():
        var = _VARS.get(key)
        if var is None:
            continue
        tokens[var] = var.set(str(value or ""))
    try:
        yield
    finally:
        for var, tok in tokens.items():
            var.reset(tok)
