"""Tests for pipeline.canonical_url (ADR-0024)."""

from __future__ import annotations

import pytest

from pipeline.canonical_url import canonicalize


class TestBasicNormalization:
    def test_lowercases_scheme_and_host(self) -> None:
        assert canonicalize("HTTPS://Example.COM/foo") == "https://example.com/foo"

    def test_strips_fragment(self) -> None:
        assert canonicalize("https://example.com/a#section") == "https://example.com/a"

    def test_strips_trailing_slash_on_path(self) -> None:
        assert canonicalize("https://example.com/foo/") == "https://example.com/foo"

    def test_keeps_root_slash(self) -> None:
        # A bare host is meaningfully different from a nested path — the
        # single root slash must survive.
        assert canonicalize("https://example.com/") == "https://example.com/"
        assert canonicalize("https://example.com") == "https://example.com/"

    def test_drops_default_ports(self) -> None:
        assert canonicalize("http://example.com:80/x") == "http://example.com/x"
        assert canonicalize("https://example.com:443/x") == "https://example.com/x"

    def test_keeps_nondefault_ports(self) -> None:
        assert canonicalize("https://example.com:8443/x") == "https://example.com:8443/x"


class TestTrackingParams:
    def test_strips_utm_family(self) -> None:
        assert canonicalize(
            "https://example.com/post?utm_source=twitter&utm_medium=social"
        ) == "https://example.com/post"

    def test_strips_fbclid_gclid(self) -> None:
        assert canonicalize(
            "https://example.com/x?fbclid=abc&gclid=def"
        ) == "https://example.com/x"

    def test_strips_substack_ref(self) -> None:
        assert canonicalize(
            "https://someone.substack.com/p/title?ref=abc123"
        ) == "https://someone.substack.com/p/title"

    def test_keeps_load_bearing_query(self) -> None:
        # The article id is the point of the URL — do not strip it.
        assert canonicalize(
            "https://news.example.com/read?id=42&utm_source=email"
        ) == "https://news.example.com/read?id=42"

    def test_sorts_remaining_params(self) -> None:
        # Same URL, different param order → same canonical.
        a = canonicalize("https://example.com/x?b=2&a=1")
        b = canonicalize("https://example.com/x?a=1&b=2")
        assert a == b == "https://example.com/x?a=1&b=2"


class TestSyndicationScenarios:
    """The whole reason this module exists — same article from different
    referrers must collapse to one canonical form."""

    def test_hn_link_and_direct_visit_same(self) -> None:
        # Reader clicked through HN → landing page with HN-supplied
        # tracking. Same article as someone who typed the URL directly.
        a = canonicalize("https://blog.example.com/2026/post?utm_source=hn")
        b = canonicalize("https://blog.example.com/2026/post")
        assert a == b

    def test_reddit_share_and_direct_same(self) -> None:
        a = canonicalize("https://arstechnica.com/foo/?ref=reddit&utm_medium=social")
        b = canonicalize("https://arstechnica.com/foo/")
        assert a == b


class TestFalsePositiveGuards:
    def test_different_paths_stay_different(self) -> None:
        assert (canonicalize("https://example.com/a")
                != canonicalize("https://example.com/b"))

    def test_different_hosts_stay_different(self) -> None:
        assert (canonicalize("https://a.example.com/x")
                != canonicalize("https://b.example.com/x"))

    def test_http_vs_https_stay_different(self) -> None:
        # Same host but different scheme is a meaningful signal — often
        # http:// URLs are legacy redirects that ARE the same content,
        # but occasionally aren't. Coarse rule: don't collapse across
        # schemes. If this bites in practice, revisit.
        assert (canonicalize("http://example.com/x")
                != canonicalize("https://example.com/x"))


class TestNullReturns:
    @pytest.mark.parametrize("url", [None, "", "   ", 123, [], {}])
    def test_bad_input_returns_none(self, url: object) -> None:
        assert canonicalize(url) is None  # type: ignore[arg-type]

    def test_non_http_scheme_returns_none(self) -> None:
        # We don't touch mailto:, feed:, magnet:, etc.
        assert canonicalize("mailto:x@example.com") is None
        assert canonicalize("feed:https://example.com/rss.xml") is None

    def test_no_host_returns_none(self) -> None:
        # A path-only URL isn't cross-source dedupable; skip.
        assert canonicalize("https:///path") is None
