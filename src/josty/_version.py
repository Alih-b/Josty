"""Single source of the package version (read by hatchling at build time)."""

from __future__ import annotations

from contextlib import suppress
from importlib.metadata import PackageNotFoundError, version

# Single version source: the static literal doubles as the pre-install fallback and
# hatchling's build-time version; installed distributions override via importlib.metadata.
__version__ = "0.6.1"
with suppress(PackageNotFoundError):
    __version__ = version("josty")
