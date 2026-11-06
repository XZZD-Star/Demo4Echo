"""Compatibility alias for :mod:`echomate_core.tools`."""

import sys

from echomate_core import tools as _target

sys.modules[__name__] = _target
