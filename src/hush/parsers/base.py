from collections.abc import Callable
from dataclasses import dataclass

from hush.extract import Failure


@dataclass(frozen=True, slots=True)
class Outcome:
    failures: tuple[Failure, ...] = ()
    test_count: int | None = None
    # Shown on the pass line next to `test_count`. Zero means absent.
    skipped: int = 0
    xfailed: int = 0
    xpassed: int = 0
    todo: int = 0
    warnings: int = 0


Parser = Callable[[str], Outcome]
