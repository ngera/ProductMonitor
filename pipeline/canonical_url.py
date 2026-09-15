"""Canonical-URL derivation for cross-source dedup (ADR-0024).

Item identity is `source:external_id` — a Reddit link post to the same
article the RSS feed also carries becomes two separate items, both flow
through classification, both land in the report. The canonical URL is
computed at normalize time and used to collapse those into one row.

Design constraints (fed by the ADR):

- Deterministic. Same input → same output every time; no randomness, no
  network calls, no HEAD requests to follow redirects.
- Coarse by design. If two URLs canonicalize the same, they ARE the
  same. False-positives are logged so operators can spot them; the
  alternative (embedding-based similarity) is heavier and out of scope.
- NULL is a valid canonical URL. Items with no URL, malformed URLs, or
  URLs we choose not to touch (e.g. reddit self-posts) get NULL and
  fall back to per-source dedup — preserving current behavior.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import parse_qsl, urlsplit, urlunsplit, urlencode

# Query params that are pure tracking noise — same target URL, different
# `?utm_source=X` per referrer. Stripping these is safe. We keep the list
# short and targeted; adding aggressive filters (like `ref`, `source`)
# risks stripping load-bearing params on some sites.
_TRACKING_PARAMS: frozenset[str] = frozenset({
    # Google Analytics / UTM family
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "utm_reader", "utm_brand", "utm_social",
    "utm_social-type",
    # Facebook / Meta
    "fbclid",
    # Google Ads
    "gclid", "gclsrc", "dclid",
    # Microsoft / Bing
    "msclkid",
    # Mailchimp
    "mc_cid", "mc_eid",
    # Substack / newsletter platforms
    "ref", "ref_src", "ref_url",
    # Reddit share-link tracking
    "share_id", "utm_name",
    # YouTube
    "feature", "si",
    # HubSpot
    "_hsenc", "_hsmi", "hsCtaTracking",
    # Yandex
    "yclid",
})

_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


def canonicalize(url: Optional[str]) -> Optional[str]:
    """Return a canonical form of `url`, or None when no meaningful
    canonicalization is possible.

    Rules:
      - Empty / non-string / malformed input → None
      - Scheme + host lowercased; default ports (:80, :443) dropped
      - Fragment (`#anchor`) stripped
      - `_TRACKING_PARAMS` removed from query string
      - Remaining query params sorted lexicographically (trackers can
        hide behind unsorted param order)
      - Trailing slash on path stripped (unless path IS `/`)
      - Non-http(s) schemes returned as-is-but-normalized; we don't try
        to canonicalize mailto:, feed:, magnet:, etc. — they're not
        the cross-syndication case we care about

    Returns None (rather than raising) for anything unparseable. Callers
    must treat None as "not deduped by URL" and fall back to per-source
    identity. This is deliberate: a wrong canonicalization that collapses
    unrelated items is worse than no canonicalization.
    """
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        # Non-http URLs (mailto:, feed:, etc.) — not the cross-syndication
        # case; skip canonicalization rather than emit a URL that means
        # something different than the input.
        return None

    host = parts.hostname
    if not host:
        return None
    host = host.lower()

    port = parts.port
    if port is not None and _DEFAULT_PORTS.get(scheme) == port:
        port = None
    netloc = host if port is None else f"{host}:{port}"

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    # Filter tracking params, sort what's left. `keep_blank_values=True`
    # so `?q=` and `?q` don't collapse into "no param" and produce
    # accidentally-equal canonicals for genuinely-different URLs.
    kept = [
        (k, v) for (k, v) in parse_qsl(parts.query, keep_blank_values=True)
        if k not in _TRACKING_PARAMS
    ]
    kept.sort()
    query = urlencode(kept)

    # Fragment intentionally dropped — `#comment-1234` on a Reddit post
    # etc. never means "different content" for our purposes.
    return urlunsplit((scheme, netloc, path, query, ""))
