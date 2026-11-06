"""Versioned entry point for shared Echo-Mate tables."""

from . import notify
from . import store


SCHEMA_VERSION = 1


def init_core_schema():
    """Create or update all shared tables in the configured database."""
    store.init_schema()
    notify.init_notify_tables()
    return SCHEMA_VERSION


__all__ = ["SCHEMA_VERSION", "init_core_schema"]
