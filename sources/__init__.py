"""Source registry. Sources are registered by their `type` value in sources.yaml."""

from __future__ import annotations

from typing import Type

from sources.base import Source

_REGISTRY: dict[str, Type[Source]] = {}


def register(type_name: str, cls: Type[Source]) -> None:
    _REGISTRY[type_name] = cls


def get_source(type_name: str) -> Source:
    if type_name not in _REGISTRY:
        raise KeyError(f"no source registered for type '{type_name}'")
    return _REGISTRY[type_name]()


def _register_builtins() -> None:
    # Imported lazily so praw stays optional until a Reddit source is actually used.
    from sources.reddit import RedditSource

    register("reddit", RedditSource)


_register_builtins()
