"""Compatibility alias for :mod:`echomate_core.credentials`."""

import sys

from echomate_core import credentials as _target

sys.modules[__name__] = _target
