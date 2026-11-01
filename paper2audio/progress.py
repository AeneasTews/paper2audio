"""Progress reporting, so the same pipeline can drive the CLI and the GUI."""

import sys
from collections.abc import Callable

# Called with a status message and, during a step with several parts (describing images,
# speaking chunks), how far along that step is, from 0 to 1.
Progress = Callable[[str, "float | None"], None]


def print_progress(message: str, fraction: float | None = None) -> None:
    print(message, file=sys.stderr)
