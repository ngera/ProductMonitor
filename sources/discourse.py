"""Discourse forum connector — public JSON endpoints on any Discourse host.

Endpoints used:
  /search.json?q=…          mention search (supports after:YYYY-MM-DD)
  /latest.json?page=N       recent topics site-wide
  /c/{slug}/{id}.json       category latest (when category slug set)
  /t/{topic_id}.json        full thread + replies

Anonymous read works on most public instances. Some admins disable it
(login_required) or rate-limit anonymous traffic — optional API key +
username in .env for those hosts.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from html import unescape
from typing import Any, Iterator, Optional

import httpx

from pipeline import http as _retry_http
from pipeline.models import RawItem
from sources.base import FetchStats, FieldSpec, Source, SourceCursor, SourceManifest

log = logging.getLogger(__name__)

_USER_AGENT = "product-monitor/0.1"
_TAG_RE = re.compile(r"<[^>]+>")


MANIFEST = SourceManifest(
    plugin_id="discourse",
    display_name="Discourse Forum",
    version="0.1.0",
    docs_url="https://docs.discourse.org/",
    help=(
        "Discourse JSON API on any public forum host (forum.cursor.com, "
        "community.home-assistant.io, …). Anonymous read works on most "
        "instances; add an API key + username only when the host disables "
        "anonymous access or rate-limits heavily."
    ),
    connection_fields=[
        FieldSpec(
            name="DISCOURSE_API_KEY",
            label="API key (optional)",
            type="secret",
            help="Only needed when anonymous read is disabled or rate-limited.",
        ),
        FieldSpec(
            name="DISCOURSE_API_USERNAME",
            label="API username (optional)",
            type="text",
            help="Discourse username paired with the API key.",
        ),
    ],
    stream_fields=[
        FieldSpec(name="name", label="Stream name", type="text", required=True,
                  placeholder="cursor-forum", help="Internal label for cursor / dedup."),
        FieldSpec(name="host", label="Forum host", type="text", required=True,
                  placeholder="forum.cursor.com",
                  help="Hostname only — no https:// prefix."),
        FieldSpec(name="mode", label="Mode", type="text", default="search",
                  help="search = mention search; latest = all new topics."),
        FieldSpec(name="query", label="Search query", type="text", default="",
                  help="Used when mode=search. Supports Discourse after:YYYY-MM-DD."),
        FieldSpec(name="category", label="Category slug", type="text", default="",
                  help="Optional category filter when mode=latest."),
        FieldSpec(name="include_replies", label="Fetch replies", type="bool", default=True,
                  help="Hydrate full threads via /t/{id}.json and emit replies."),
        FieldSpec(name="max_pages", label="Max list pages", type="number", default=3,
                  help="Pages of search/latest/category results per run."),
    ],
    identifier_field="host",
    supports_bulk_add=True,
    source_category="custom_source",
    content_types=["user_feedback"],
)


def _normalize_host(host: str) -> str:
    h = (host or "").strip().lower()
    h = re.sub(r"^https?://", "", h)
    h = h.split("/")[0]  # drop path if pasted
    return h.rstrip("/")


def normalize_host(host: str) -> str:
    """Public alias used by the wizard when normalizing typed hosts."""
    return _normalize_host(host)


def _registrable_domain_from_url(url: str) -> str:
    """Best-effort apex host from a product URL (e.g. www.snowflake.com → snowflake.com)."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        from urllib.parse import urlparse
        host = (urlparse(raw).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""
    return host


def _parse_ts(raw: str) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _strip_html(html: str) -> str:
    if not html:
        return ""
    text = _TAG_RE.sub(" ", html)
    return unescape(re.sub(r"\s+", " ", text)).strip()


def _auth_headers() -> dict[str, str]:
    key = (os.environ.get("DISCOURSE_API_KEY") or "").strip()
    user = (os.environ.get("DISCOURSE_API_USERNAME") or "").strip()
    if key and user:
        return {"Api-Key": key, "Api-Username": user}
    return {}


class DiscourseSource(Source):
    name = "discourse"

    def __init__(self) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": _USER_AGENT, **_auth_headers()},
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        )

    def fetch_since(
        self, cursor: SourceCursor, config: dict[str, Any], stats: FetchStats
    ) -> Iterator[RawItem]:
        host = _normalize_host(str(config.get("host") or ""))
        if not host:
            raise ValueError("discourse stream config missing required 'host'")

        mode = (config.get("mode") or "search").strip().lower()
        include_replies = bool(config.get("include_replies", True))
        max_pages = max(1, int(config.get("max_pages") or 3))
        display = config.get("name") or host
        floor: float = float(cursor.cursor_ts or 0)
        newest_seen = floor

        topic_ids: list[int] = []
        if mode == "search":
            query = (config.get("query") or "").strip()
            if not query:
                raise ValueError("discourse mode=search requires non-empty 'query'")
            if floor > 0:
                after = datetime.fromtimestamp(floor, tz=timezone.utc).strftime("%Y-%m-%d")
                if "after:" not in query.lower():
                    query = f"{query} after:{after}"
            topic_ids = self._search_topics(host, query, max_pages)
        elif mode == "latest":
            category = (config.get("category") or "").strip()
            topic_ids = self._list_topics(host, category, max_pages)
        else:
            raise ValueError(f"discourse unknown mode={mode!r}; use search or latest")

        seen_topics: set[int] = set()
        for tid in topic_ids:
            if tid in seen_topics:
                continue
            seen_topics.add(tid)
            try:
                payload = self._get_json(host, f"/t/{tid}.json")
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (403, 401):
                    raise RuntimeError(
                        f"Discourse host {host} refused anonymous read ({e.response.status_code}). "
                        "Set DISCOURSE_API_KEY and DISCOURSE_API_USERNAME in .env."
                    ) from e
                log.warning("discourse topic fetch failed host=%s id=%s: %s", host, tid, e)
                continue

            for item in self._topic_to_items(host, payload, display, include_replies, floor):
                ts = item.created_at.timestamp()
                if ts > newest_seen:
                    newest_seen = ts
                yield item

        if newest_seen > floor:
            cursor.cursor_ts = newest_seen

    def _base_url(self, host: str) -> str:
        return f"https://{host}"

    def _get_json(self, host: str, path: str, *, params: Optional[dict] = None) -> dict:
        resp = _retry_http.request_with_retry(
            lambda: self._client.get(f"{self._base_url(host)}{path}", params=params or {}),
            source_id="discourse",
        )
        resp.raise_for_status()
        return resp.json()

    def _search_topics(self, host: str, query: str, max_pages: int) -> list[int]:
        ids: list[int] = []
        for page in range(1, max_pages + 1):
            data = self._get_json(host, "/search.json", params={"q": query, "page": page})
            for topic in data.get("topics") or []:
                tid = topic.get("id")
                if tid is not None:
                    ids.append(int(tid))
            posts = data.get("posts") or []
            for post in posts:
                tid = post.get("topic_id")
                if tid is not None:
                    ids.append(int(tid))
            if not (data.get("topics") or data.get("posts")):
                break
        return ids

    def _list_topics(self, host: str, category: str, max_pages: int) -> list[int]:
        ids: list[int] = []
        for page in range(0, max_pages):
            if category:
                path = f"/c/{category}.json"
                params = {"page": page} if page else None
            else:
                path = "/latest.json"
                params = {"page": page + 1}
            data = self._get_json(host, path, params=params)
            topics = data.get("topic_list", {}).get("topics") or data.get("topics") or []
            if not topics:
                break
            for topic in topics:
                tid = topic.get("id")
                if tid is not None:
                    ids.append(int(tid))
        return ids

    def _topic_to_items(
        self,
        host: str,
        payload: dict[str, Any],
        display: str,
        include_replies: bool,
        floor: float,
    ) -> Iterator[RawItem]:
        slug = payload.get("slug") or "topic"
        topic_id = int(payload["id"])
        title = payload.get("title") or ""
        views = int(payload.get("views") or 0)
        posts_count = int(payload.get("posts_count") or 0)
        like_count = int(payload.get("like_count") or 0)

        stream = payload.get("post_stream") or {}
        posts = stream.get("posts") or []
        if not posts:
            return

        op = posts[0]
        op_ts = _parse_ts(op.get("created_at") or "").timestamp()
        if op_ts <= floor:
            return

        op_item = self._post_to_item(
            host, slug, topic_id, title, op,
            parent_external_id=None,
            display=display,
            engagement={
                "posts_count": posts_count,
                "like_count": like_count,
                "views": views,
            },
        )
        yield op_item

        if not include_replies:
            return

        for post in posts[1:]:
            ts = _parse_ts(post.get("created_at") or "").timestamp()
            if ts <= floor:
                continue
            yield self._post_to_item(
                host, slug, topic_id, title, post,
                parent_external_id=f"{host}:topic:{topic_id}",
                display=display,
                engagement={"likes": int(post.get("like_count") or 0)},
                parent_context={"title": title, "body": _strip_html(op.get("cooked") or "")[:500]},
            )

    def _post_to_item(
        self,
        host: str,
        slug: str,
        topic_id: int,
        title: str,
        post: dict[str, Any],
        *,
        parent_external_id: Optional[str],
        display: str,
        engagement: dict[str, Any],
        parent_context: Optional[dict] = None,
    ) -> RawItem:
        post_number = int(post.get("post_number") or 1)
        post_id = int(post.get("id") or post_number)
        author = post.get("username") or post.get("name")
        body = _strip_html(post.get("cooked") or post.get("raw") or "")
        created = _parse_ts(post.get("created_at") or "")
        url = f"https://{host}/t/{slug}/{topic_id}/{post_number}"

        raw: dict[str, Any] = {"topic_id": topic_id, "post_id": post_id}
        if parent_context:
            raw["parent_context"] = parent_context

        return RawItem(
            source="discourse",
            source_display_name=f"Discourse ({host})",
            external_id=f"{host}:topic:{topic_id}:post:{post_id}",
            url=url,
            parent_external_id=parent_external_id,
            author=author,
            created_at=created,
            title=title if parent_external_id is None else None,
            body=body,
            content_type="user_feedback",
            engagement=engagement,
            raw=raw,
        )

    # --- discovery (ADR-0030) ------------------------------------------------

    def discover_streams(self, profile_facts, max_candidates=8):
        """Suggest Discourse forum hosts for this product (ADR-0030).

        No universal Discourse search API exists, so this asks the
        assistant LLM for likely hostnames (same pattern as reddit_rss /
        stackex). Heuristic fallbacks from the product URL
        (`forum.` / `community.` / `discuss.` + apex domain) keep the
        Forum host field from staying blank when the LLM is empty.
        """
        from sources.base import StreamCandidate
        try:
            from pipeline import stream_suggestions as _ss
        except Exception:
            return self._fallback_host_candidates(profile_facts, max_candidates)

        suggestions = _ss.suggest_stream_identifiers(
            profile_facts=profile_facts,
            plugin_id=self.name,
            field_name="host",
            field_help=(
                "Discourse forum hostname only — e.g. forum.cursor.com or "
                "community.home-assistant.io. No https:// prefix."
            ),
            product_id_for_budget=str(
                profile_facts.get("product_id")
                or profile_facts.get("slug")
                or ""
            ),
        )
        display = (profile_facts.get("display") or "").strip()
        out: list[StreamCandidate] = []
        seen: set[str] = set()
        for s in suggestions or []:
            host = normalize_host(str(s.get("value") or ""))
            if not host or host in seen:
                continue
            if "." not in host:
                continue  # Discourse needs a real hostname, not a bare word
            seen.add(host)
            rationale = str(s.get("rationale") or "").strip()
            cfg: dict[str, Any] = {
                "host": host,
                "name": f"discourse-{host.replace('.', '-')}",
                "mode": "search",
            }
            if display:
                cfg["query"] = display
            out.append(StreamCandidate(
                stream_config=cfg,
                display_name=host,
                rationale=rationale,
                quality_signal="",
                provider_url=f"https://{host}",
            ))
            if len(out) >= max_candidates:
                break
        if not out:
            return self._fallback_host_candidates(profile_facts, max_candidates)
        return out

    @staticmethod
    def _fallback_host_candidates(profile_facts, max_candidates: int = 8):
        """URL-derived host guesses when the assistant returns nothing."""
        from sources.base import StreamCandidate
        apex = _registrable_domain_from_url(str(profile_facts.get("url") or ""))
        if not apex or "." not in apex:
            return []
        display = (profile_facts.get("display") or "").strip()
        out: list[StreamCandidate] = []
        for prefix in ("forum", "community", "discuss", "meta"):
            host = f"{prefix}.{apex}"
            cfg: dict[str, Any] = {
                "host": host,
                "name": f"discourse-{host.replace('.', '-')}",
                "mode": "search",
            }
            if display:
                cfg["query"] = display
            out.append(StreamCandidate(
                stream_config=cfg,
                display_name=host,
                rationale=f"Common Discourse hostname pattern on {apex}",
                quality_signal="",
                provider_url=f"https://{host}",
            ))
            if len(out) >= max_candidates:
                break
        return out

    def __del__(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
