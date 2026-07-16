"""Tests for filter Stage A heuristics (§4.4)."""

from pipeline.filter import WATCHLIST_RE, _canonical_url, _drop_reason


def _item(**kw):
    base = {
        "id": "reddit:abc",
        "url": "https://reddit.com/r/Windows11/comments/abc/",
        "title": "A reasonably long descriptive title here",
        "body": "x" * 100,
        "engagement_json": '{"upvotes": 50, "comment_count": 10}',
    }
    base.update(kw)
    return base


def test_passes_normal_item():
    assert _drop_reason(_item(), 50, 5, set(), [], 4) is None


def test_drops_too_short():
    it = _item(body="short", title="hi")
    assert _drop_reason(it, 50, 5, set(), [], 4) == "too_short"


def test_drops_deleted():
    it = _item(body="[deleted]", title="")
    assert _drop_reason(it, 50, 5, set(), [], 4) == "deleted_or_empty"


def test_drops_low_engagement():
    it = _item(body="x" * 100, title="hi", engagement_json='{"upvotes": 1, "comment_count": 0}')
    assert _drop_reason(it, 50, 5, set(), [], 4) == "low_engagement"


def test_watchlist_overrides_low_engagement():
    it = _item(body="Issue after KB5036980 update broke audio", title="bug",
               engagement_json='{"upvotes": 0, "comment_count": 0}')
    assert _drop_reason(it, 50, 5, set(), [], 4) is None


def test_duplicate_url():
    seen = {_canonical_url("https://reddit.com/r/Windows11/comments/abc/")}
    assert _drop_reason(_item(), 50, 5, seen, [], 4) == "duplicate_url"


def test_watchlist_regex_matches_kb_cve_build():
    assert WATCHLIST_RE.search("KB5036980")
    assert WATCHLIST_RE.search("CVE-2024-1234")
    assert WATCHLIST_RE.search("build 26100.4061")


def test_canonical_url_strips_tracking_params_and_trailing_slash():
    # Known tracking params (utm_source, fbclid, etc.) are stripped.
    a = _canonical_url("https://reddit.com/r/x/comments/abc/?utm_source=twitter")
    b = _canonical_url("https://reddit.com/r/x/comments/abc")
    assert a == b


def test_canonical_url_preserves_identity_params():
    # Identity params (id, v, etc.) MUST NOT be stripped — otherwise every
    # HN item collapses to news.ycombinator.com/item (see the pre-fix bug
    # in git history).
    a = _canonical_url("https://news.ycombinator.com/item?id=48802571")
    b = _canonical_url("https://news.ycombinator.com/item?id=48805358")
    assert a != b, "identity params must differentiate items"
    assert "id=48802571" in a
    assert "id=48805358" in b
