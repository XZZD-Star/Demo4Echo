"""Compatibility alias for :mod:`echomate_core.observer`."""

import sys

from echomate_core import observer as _target

sys.modules[__name__] = _target
