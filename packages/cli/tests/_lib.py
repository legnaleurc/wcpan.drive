from typing import cast
from unittest.mock import AsyncMock


def create_amock[T](t: type[T]) -> T:
    return cast(T, AsyncMock(spec=t))


def aexpect(o: object) -> AsyncMock:
    return cast(AsyncMock, o)
