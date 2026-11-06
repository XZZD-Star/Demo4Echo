"""Shared Echo-Mate business and storage capabilities."""

from .schema import SCHEMA_VERSION, init_core_schema

__version__ = "0.1.0"

__all__ = ["SCHEMA_VERSION", "__version__", "init_core_schema"]
