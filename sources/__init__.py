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


def available_source_types() -> list[str]:
    """Sorted list of source `type` values the registry knows about.

    Used by the admin UI to populate the "Add source" type picker so users
    can only add source instances of types this build supports.
    """
    return sorted(_REGISTRY.keys())


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

    try:
        from sources.stackex import StackExchangeSource
        register("stackex", StackExchangeSource)
    except Exception:
        pass

    try:
        from sources.apple_appstore import AppleAppStoreSource
        register("apple_appstore", AppleAppStoreSource)
    except Exception:
        pass

    try:
        from sources.producthunt import ProductHuntSource
        register("producthunt", ProductHuntSource)
    except Exception:
        pass

    try:
        from sources.rss import RssSource
        register("rss", RssSource)
    except Exception:
        pass

    try:
        from sources.youtube_comments import YouTubeCommentsSource
        register("youtube_comments", YouTubeCommentsSource)
    except Exception:
        pass


_register_builtins()
