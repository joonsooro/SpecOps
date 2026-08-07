from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID, uuid4


class Clock(Protocol):
    def now(self) -> datetime: ...


class UuidGenerator(Protocol):
    def new(self) -> UUID: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass(frozen=True)
class FrozenClock:
    instant: datetime

    def now(self) -> datetime:
        if self.instant.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware instant")
        return self.instant.astimezone(timezone.utc)


class RandomUuidGenerator:
    def new(self) -> UUID:
        return uuid4()


class SequenceIdGenerator:
    def __init__(self, values: list[UUID]) -> None:
        self._values: Iterator[UUID] = iter(values)

    def new(self) -> UUID:
        return next(self._values)

