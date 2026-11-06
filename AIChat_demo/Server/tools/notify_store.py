"""Compatibility alias for :mod:`echomate_core.notify`."""

import sys

from echomate_core import notify as _target

sys.modules[__name__] = _target
