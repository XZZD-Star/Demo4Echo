"""Compatibility alias for :mod:`echomate_core.memory`."""

import sys

from echomate_core import memory as _target

sys.modules[__name__] = _target
