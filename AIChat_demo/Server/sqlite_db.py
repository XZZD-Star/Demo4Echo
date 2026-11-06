"""Compatibility alias for :mod:`echomate_core.db`."""

import sys

from echomate_core import db as _target

sys.modules[__name__] = _target
