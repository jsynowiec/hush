from collections.abc import Callable
from dataclasses import dataclass

from hush.extract import Failure


@dataclass(frozen=True, slots=True)
class Outcome:
    failures: tuple[Failure, ...] = ()
    test_count: int | None = None


Parser = Callable[[str], Outcome]
