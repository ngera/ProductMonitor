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
    """Register first-party sources.

    Each import is lazy and wrapped — a missing optional dep on one source
    (e.g. praw / feedparser) should never block the others from registering.
    """
    try:
        from sources.reddit import RedditSource
        register("reddit", RedditSource)
    except Exception:
        pass

    try:
        from sources.hn import HackerNewsSource
        register("hn", HackerNewsSource)
    except Exception:
        pass

    try:
        from sources.github_issues import GitHubIssuesSource
        register("github_issues", GitHubIssuesSource)
    except Exception:
        pass

    try:
        from sources.microsoft_community import MicrosoftCommunitySource
        register("microsoft_community", MicrosoftCommunitySource)
    except Exception:
        pass


_register_builtins()
