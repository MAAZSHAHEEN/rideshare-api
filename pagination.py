"""Total counts for paginated list endpoints (G7).

List responses keep their JSON array shape (pinned by contract tests); the
number of rows matching the filters, ignoring limit/offset, travels in the
``X-Total-Count`` response header, which CORS exposes to the browser (main.py).
The count is a second statement in the same session under READ COMMITTED, so a
concurrent write between the page query and the count can make them differ by
the rows that write touched; clients should treat it as a pagination hint.
"""

from collections.abc import Sequence
from typing import TypeVar

from fastapi import Response

T = TypeVar("T")

TOTAL_COUNT = "X-Total-Count"
TOTAL_COUNT_HEADER = {200: {"headers": {TOTAL_COUNT: {
    "description": "Rows matching the filters, ignoring limit and offset.",
    "schema": {"type": "integer", "minimum": 0},
}}}}


def with_total(response: Response, page: tuple[Sequence[T], int]) -> Sequence[T]:
    items, total = page
    response.headers[TOTAL_COUNT] = str(total)
    return items
