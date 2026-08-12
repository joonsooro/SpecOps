"""Versioned Workshop Interaction Protocol runtime surface.

The executable contracts in :mod:`specops_workshop.v4.contracts` are generated
from the approved V4 bundle.  Runtime code belongs here; the generated contract
module itself must never be edited by hand.
"""

from .contracts import PROTOCOL_VERSION

__all__ = ["PROTOCOL_VERSION"]
