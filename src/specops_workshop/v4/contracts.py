"""Compatibility import for the shared generated Workshop protocol contracts."""

from specops_contracts.workshop_v1 import *  # noqa: F401,F403
from specops_contracts import workshop_v1 as _source

# The generated module intentionally exposes only a curated ``__all__``.  The
# runtime compiler also needs concrete supporting classes named by that source.
globals().update({name: value for name, value in vars(_source).items() if not name.startswith("__")})
