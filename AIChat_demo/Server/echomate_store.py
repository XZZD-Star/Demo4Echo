"""Compatibility alias for :mod:`echomate_core.store`."""

import sys

from echomate_core import store as _target

sys.modules[__name__] = _target
