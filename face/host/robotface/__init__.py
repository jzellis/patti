"""Host-side (Raspberry Pi) library for driving Patti's face."""

import os
import sys

# The protocol and expression geometry are shared verbatim with the Pico, so
# they live in face/shared/ and get copied onto the device at deploy time.
_SHARED = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "shared")
if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)

import protocol        # noqa: E402,F401
import expressions     # noqa: E402,F401

__all__ = ["protocol", "expressions", "render", "client"]
