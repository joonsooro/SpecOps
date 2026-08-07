"""Public, side-effect-free package boundary for the SpecOps workflow kernel."""

from .errors import DomainError, ErrorCode
from .models import *  # noqa: F403 - the models module owns the public DTO inventory
from .ports import Clock, FrozenClock, SequenceIdGenerator, SystemClock, UuidGenerator
from .service import WorkflowService
def migrate(database_url: str) -> None:
    """Upgrade a configured database without introducing package-import side effects."""
    from .persistence import migrate as _migrate
    _migrate(database_url)

__all__ = [
    "Clock",
    "DomainError",
    "ErrorCode",
    "FrozenClock",
    "SequenceIdGenerator",
    "SystemClock",
    "UuidGenerator",
    "WorkflowService",
    "migrate",
]
