"""Fetch title/body for a snippet from a public URL (or the product warehouse).

Used by the webui "Add snippet from URL" flow. Prefer the warehouse when the
item was already ingested; otherwise hit public endpoints (Reddit JSON, HN
Algolia) or fall back to HTML text extraction. No API keys required.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import httpx

_USER_AGENT = "ProductMonitor/1.0 (+local; snippet-fetch)"
_TIMEOUT_S = 20.0
_MAX_BODY_CHARS = 8000

_REDDIT_COMMENTS = re.compile(
    r"(?:old\.|www\.|new\.)?reddit\.com/r/([^/]+)/comments/([a-z0-9]+)",
    re.I,
)
_HN_ITEM = re.compile(r"(?:news\.ycombinator\.com/item\?id=|hn\.algolia\.com)", re.I)


@dataclass
class FetchedSnippet:
    """Content resolved for a source URL."""

    ok: bool
    url: str
    title: str = ""
    body: str = ""
    source_display_name: str = ""
    error: str = ""
    from_warehouse: bool = False


def fetch_snippet_content(
    url: str,
    *,
    product_id: Optional[str] = None,
) -> FetchedSnippet:
    """Resolve title/body for ``url``.

    Order: warehouse (if ``product_id``) → Reddit JSON → HN → generic HTML.
    """
    url = (url or "").strip()
    if not url:
        return FetchedSnippet(ok=False, url="", error="URL is required")
    if not url.startswith(("http://", "https://")):
        return FetchedSnippet(ok=False, url=url, error="URL must start with http:// or https://")

    if product_id:
        hit = _from_warehouse(product_id, url)
        if hit is not None:
            return hit

    try:
        if _REDDIT_COMMENTS.search(url):
            return _fetch_reddit(url)
        hn_id = _hn_item_id(url)
        if hn_id:
            return _fetch_hn(url, hn_id)
        return _fetch_generic_html(url)
    except Exception as e:
        return FetchedSnippet(ok=False, url=url, error=f"Fetch failed: {e}")


def _warehouse_path(product_id: str) -> Path:
    from pipeline.config import app_config, resolve_path
    return resolve_path(app_config()["paths"]["data_root"]) / product_id / "warehouse.duckdb"


def _from_warehouse(product_id: str, url: str) -> Optional[FetchedSnippet]:
    """Return a FetchedSnippet if the warehouse already has this URL."""
    path = _warehouse_path(product_id)
    if not path.exists():
        return None
    try:
        import duckdb
        con = duckdb.connect(str(path), read_only=True)
        try:
            row = con.execute(
                "SELECT title, body, source_display_name, url, canonical_url "
                "FROM items WHERE url = ? OR canonical_url = ? "
                "OR url = ? OR canonical_url = ? "
                "LIMIT 1",
                [url, url, url.rstrip("/"), url.rstrip("/")],
            ).fetchone()
        finally:
            con.close()
    except Exception:
        return None
    if not row:
        return None
    title, body, display, item_url, _canon = row
    body_s = (body or "").strip()
    if not body_s and not (title or "").strip():
        return None
    return FetchedSnippet(
        ok=True,
        url=item_url or url,
        title=(title or "").strip(),
        body=body_s[:_MAX_BODY_CHARS],
        source_display_name=(display or "").strip() or _display_from_url(url),
        from_warehouse=True,
    )


def _display_from_url(url: str) -> str:
    m = _REDDIT_COMMENTS.search(url)
    if m:
        return f"r/{m.group(1)}"
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if "ycombinator" in host or host == "news.ycombinator.com":
        return "Hacker News"
    return host or "web"


def _reddit_json_url(url: str) -> str:
    """Convert a comments URL to Reddit's public JSON endpoint."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path.endswith(".json"):
        return f"https://www.reddit.com{path}?{parsed.query}".rstrip("?")
    return f"https://www.reddit.com{path}.json"


def _fetch_reddit(url: str) -> FetchedSnippet:
    m = _REDDIT_COMMENTS.search(url)
    sub = m.group(1) if m else ""
    json_url = _reddit_json_url(url)
    with httpx.Client(
        timeout=_TIMEOUT_S,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        resp = client.get(json_url)
        if resp.status_code == 403 or resp.status_code == 429:
            return FetchedSnippet(
                ok=False, url=url,
                error=f"Reddit blocked the request (HTTP {resp.status_code}). "
                      "Try again later or paste the post as free-form text.",
            )
        resp.raise_for_status()
        data = resp.json()

    # Listing shape: [post_listing, comments_listing]
    post: dict[str, Any] = {}
    if isinstance(data, list) and data:
        children = (data[0].get("data") or {}).get("children") or []
        if children:
            post = (children[0].get("data") or {})
    elif isinstance(data, dict):
        children = (data.get("data") or {}).get("children") or []
        if children:
            post = (children[0].get("data") or {})

    title = (post.get("title") or "").strip()
    body = (post.get("selftext") or "").strip()
    if not body:
        # Link posts: use the outbound URL as body context.
        outbound = (post.get("url_overridden_by_dest") or post.get("url") or "").strip()
        if outbound and not outbound.startswith("https://www.reddit.com"):
            body = outbound
    if not title and not body:
        return FetchedSnippet(ok=False, url=url, error="Reddit returned no post content")
    return FetchedSnippet(
        ok=True,
        url=url,
        title=title,
        body=body[:_MAX_BODY_CHARS],
        source_display_name=f"r/{sub}" if sub else _display_from_url(url),
    )


def _hn_item_id(url: str) -> Optional[str]:
    parsed = urlparse(url)
    if "ycombinator.com" in (parsed.netloc or "").lower():
        qs = parse_qs(parsed.query or "")
        ids = qs.get("id") or []
        if ids and ids[0].isdigit():
            return ids[0]
    return None


def _fetch_hn(url: str, item_id: str) -> FetchedSnippet:
    api = f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json"
    with httpx.Client(timeout=_TIMEOUT_S, headers={"User-Agent": _USER_AGENT}) as client:
        resp = client.get(api)
        resp.raise_for_status()
        data = resp.json() or {}
    title = (data.get("title") or "").strip()
    body = (data.get("text") or "").strip()
    # Firebase returns HTML entities in text; strip tags lightly.
    if body:
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"\s+", " ", body).strip()
    if not body and data.get("url"):
        body = str(data["url"]).strip()
    if not title and not body:
        return FetchedSnippet(ok=False, url=url, error="HN item has no title or text")
    return FetchedSnippet(
        ok=True,
        url=url,
        title=title,
        body=body[:_MAX_BODY_CHARS],
        source_display_name="Hacker News",
    )


def _fetch_generic_html(url: str) -> FetchedSnippet:
    try:
        from bs4 import BeautifulSoup
    except Exception as e:
        return FetchedSnippet(ok=False, url=url, error=f"HTML parser unavailable: {e}")

    with httpx.Client(
        timeout=_TIMEOUT_S,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        resp = client.get(url)
        resp.raise_for_status()
        html = resp.text

    soup = BeautifulSoup(html, "html.parser")
    title = ""
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og["content"].strip()
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()

    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
        tag.decompose()
    # Prefer <article> / <main> when present.
    root = soup.find("article") or soup.find("main") or soup.body or soup
    lines = [ln.strip() for ln in root.get_text(separator="\n").splitlines()]
    body = "\n".join(ln for ln in lines if ln)
    body = body[:_MAX_BODY_CHARS]
    if not title and not body:
        return FetchedSnippet(ok=False, url=url, error="Page had no extractable text")
    return FetchedSnippet(
        ok=True,
        url=url,
        title=title,
        body=body,
        source_display_name=_display_from_url(url),
    )
