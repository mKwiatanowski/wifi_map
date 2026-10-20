from typing import Generic, TypeVar

T = TypeVar("T")


class Registry(Generic[T]):  # noqa: UP046 - explicit Generic kept per interface spec
    def __init__(self) -> None:
        self._items: dict[str, T] = {}

    def register(self, name: str, item: T) -> None:
        if name in self._items:
            raise ValueError(f"already registered: {name}")
        self._items[name] = item

    def get(self, name: str) -> T:
        return self._items[name]

    def items(self) -> list[tuple[str, T]]:
        return list(self._items.items())

    def names(self) -> list[str]:
        return list(self._items)
