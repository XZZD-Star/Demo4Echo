"""Compatibility alias for :mod:`echomate_core.actions`."""

import sys

from echomate_core import actions as _target

sys.modules[__name__] = _target
