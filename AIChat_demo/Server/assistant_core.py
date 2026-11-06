"""Compatibility alias for :mod:`echomate_core.assistant`."""

import sys

from echomate_core import assistant as _target

sys.modules[__name__] = _target
