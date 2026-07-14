"""Stack Exchange connector via the public 2.3 REST API (SOURCE_STACK_EXCHANGE.md).

Anonymous mode gives 300 req/day; an API key (register at
stackapps.com/apps/oauth/register — no approval wait) raises it to 10K/day.
Set STACKEX_KEY in .env for the higher quota. Nothing else is required.

Stream config:

    streams:
      - name: superuser-windows
        site: superuser                  # required. See SUPPORTED_SITES.
        tags: [windows-11, windows-10]   # AND-joined via ';' per SE spec
        unanswered_only: false           # true = only questions without accepted_answer
        hydrate_answers: false           # true = also emit answers as child items
        max_pages: 5                     # safety cap; SE default page size 100
        engagement_threshold: 0          # min (score + answer_count) to yield

Cursor is the epoch-second `creation_date` of the newest question seen. We
advance to the max so dedup via seen_ids catches the small overlap on the
next run.

Backoff handling: SE responses may include a `backoff` field. When present,
we sleep for that many seconds before the next API call. Ignoring it gets
your key throttled.

CC BY-SA attribution: bodies from SE are CC BY-SA 4.0. The report template's
_item_attribution partial already renders author + link (satisfies attribution).
Do not strip either from the rendered output.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

import httpx
from bs4 import BeautifulSoup

from pipeline.models import RawItem
from sources.base import FetchStats, Source, SourceCursor

log = logging.getLogger(__name__)

_BASE_URL = "https://api.stackexchange.com/2.3"
_USER_AGENT = "customer-feedback-monitor/0.1"
_DEFAULT_PAGE_SIZE = 100
_DEFAULT_MAX_PAGES = 5
# withbody: include question body (default filter omits it — easy bug).
# See docs/SOURCE_STACK_EXCHANGE.md §6.2
_DEFAULT_FILTER = "withbody"

# Sites we know the display name for. Anything not in this dict falls back
# to Title-casing the site slug. Extend as we start ingesting from more SEs.
_SITE_DISPLAY = {
    "superuser":    "Super User",
    "stackoverflow": "Stack Overflow",
    "serverfault":  "Server Fault",
    "apple":        "Ask Different",
    "unix":         "Unix & Linux Stack Exchange",
    "askubuntu":    "Ask Ubuntu",
    "gaming":       "Arqade",
    "electronics":  "Electrical Engineering Stack Exchange",
}


def _site_display_name(site: str) -> str:
    return _SITE_DISPLAY.get(site, site.replace("_", " ").title())


def _strip_html(html: str) -> str:
    """Strip HTML to plain text but keep code content readable.

    SE bodies include <code>/<pre> blocks that are often the most informative
    part of a question. BeautifulSoup's get_text() with a newline separator
    preserves the code content while removing tags.
    """
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
        return soup.get_text(separator="\n", strip=True)
    except Exception:
        return html  # best-effort: return raw HTML rather than crashing the run


def _q_to_item(q: dict[str, Any], site: str) -> RawItem:
    """Map one /questions response entry to a RawItem."""
    owner = q.get("owner") or {}
    return RawItem(
        source="stackex",
        source_display_name=_site_display_name(site),
        external_id=str(q["question_id"]),
        url=q["link"],
        parent_external_id=None,
        author=owner.get("display_name"),
        created_at=datetime.fromtimestamp(int(q["creation_date"]), tz=timezone.utc),
        title=q.get("title"),
        body=_strip_html(q.get("body") or ""),
        engagement={
            "score": q.get("score", 0),
            "view_count": q.get("view_count", 0),
            "answer_count": q.get("answer_count", 0),
            "is_answered": bool(q.get("is_answered")),
            "accepted_answer_id": q.get("accepted_answer_id"),
            "asker_reputation": owner.get("reputation", 0),
        },
        raw={
            "tags": q.get("tags") or [],
            "site": site,
            "is_comment": False,
        },
    )


def _a_to_item(a: dict[str, Any], site: str, parent_q: dict[str, Any]) -> RawItem:
    """Map one /answers response entry to a RawItem (as a child of its question).

    parent_q is the original question payload; we use its title + body snippet
    as parent context so the classifier can score answers with topic in mind.
    """
    owner = a.get("owner") or {}
    return RawItem(
        source="stackex",
        source_display_name=_site_display_name(site),
        external_id=f"a{a['answer_id']}",  # namespaced so it can't collide with a question_id
        url=f"{parent_q['link']}#{a['answer_id']}",
        parent_external_id=str(a["question_id"]),
        author=owner.get("display_name"),
        created_at=datetime.fromtimestamp(int(a["creation_date"]), tz=timezone.utc),
        title=None,  # answers don't carry a title
        body=_strip_html(a.get("body") or ""),
        engagement={
            "score": a.get("score", 0),
            "is_accepted": bool(a.get("is_accepted")),
            "answerer_reputation": owner.get("reputation", 0),
        },
        raw={
            "site": site,
            "is_comment": False,
            "parent_context": {
                "title": parent_q.get("title"),
                "body_snippet": (_strip_html(parent_q.get("body") or ""))[:500],
            },
        },
    )


class StackExchangeSource(Source):
    name = "stackex"

    def __init__(self) -> None:
        self._client = httpx.Client(
            base_url=_BASE_URL,
            headers={"User-Agent": _USER_AGENT},
            timeout=httpx.Timeout(20.0),
        )
        # SE returns a `backoff` field when it wants us to slow down. Honored
        # on the next request. Docs §6.1.
        self._backoff_until: float = 0.0

    def _sleep_if_backoff(self) -> None:
        now = time.monotonic()
        if now < self._backoff_until:
            time.sleep(self._backoff_until - now)

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        self._sleep_if_backoff()
        # Inject key from env at call time so users can rotate .env between runs.
        key = os.environ.get("STACKEX_KEY")
        if key:
            params = {**params, "key": key}
        resp = self._client.get(path, params=params)
        resp.raise_for_status()
        payload = resp.json()
        if "backoff" in payload:
            try:
                self._backoff_until = time.monotonic() + float(payload["backoff"])
            except (TypeError, ValueError):
                pass
        return payload

    def fetch_since(
        self, cursor: SourceCursor, config: dict[str, Any], stats: FetchStats
    ) -> Iterator[RawItem]:
        site = config.get("site")
        if not site:
            raise ValueError("stackex stream config missing required 'site'")

        tags = config.get("tags") or []
        unanswered_only = bool(config.get("unanswered_only", False))
        hydrate_answers = bool(config.get("hydrate_answers", False))
        max_pages = int(config.get("max_pages", _DEFAULT_MAX_PAGES))
        page_size = int(config.get("hits_per_page", _DEFAULT_PAGE_SIZE))
        engagement_threshold = int(config.get("engagement_threshold", 0))
        display_name = config.get("name") or f"stackex-{site}"

        floor: float = float(cursor.cursor_ts or 0)
        newest_seen = floor

        # Build the /questions params. `tagged` uses ';' as AND per SE docs.
        params_base: dict[str, Any] = {
            "site": site,
            "sort": "creation",
            "order": "desc",
            "pagesize": page_size,
            "filter": _DEFAULT_FILTER,
        }
        if tags:
            params_base["tagged"] = ";".join(tags)
        if floor > 0:
            # SE `fromdate` is inclusive; we'll rely on dedup for the overlap.
            params_base["fromdate"] = int(floor)
        if unanswered_only:
            # `accepted=False` = only questions without an accepted answer.
            # This is the "gold unsolved pain" slice from the design doc.
            params_base["accepted"] = "False"

        # Collect kept questions so we can batch-fetch answers at the end.
        kept_questions: list[dict[str, Any]] = []

        for page in range(1, max_pages + 1):
            payload = self._get("/questions", {**params_base, "page": page})
            items = payload.get("items") or []
            if not items:
                break

            for q in items:
                # Cheap engagement gate before yielding — keeps low-quality
                # ingest down without asking the pipeline's filter stage.
                signal = int(q.get("score", 0)) + int(q.get("answer_count", 0))
                if signal < engagement_threshold:
                    continue

                item = _q_to_item(q, site)
                ts = item.created_at.timestamp()
                if ts > newest_seen:
                    newest_seen = ts
                kept_questions.append(q)
                yield item

            # SE tells us explicitly whether more pages exist. Trust it over
            # counting items so we don't over-fetch at end of stream.
            if not payload.get("has_more"):
                break

            # Quota watchdog: if we're down to <100 requests remaining, stop
            # rather than exhausting the whole day's budget on one stream.
            remaining = payload.get("quota_remaining")
            if remaining is not None and int(remaining) < 100:
                stats.ceiling_hits.append(
                    (f"stackex:{display_name}:quota", float(int(remaining)))
                )
                break

        if hydrate_answers and kept_questions:
            yield from self._hydrate_answers(site, kept_questions, display_name, stats)

        if newest_seen > floor:
            cursor.cursor_ts = newest_seen

    def _hydrate_answers(
        self,
        site: str,
        questions: list[dict[str, Any]],
        display_name: str,
        stats: FetchStats,
    ) -> Iterator[RawItem]:
        """Batch-fetch answers for a set of questions (up to 100 ids per call).

        The SE endpoint is /questions/{ids}/answers where ids is ';'-joined.
        We index the parent question by id so each answer can carry its
        parent context.
        """
        q_by_id = {q["question_id"]: q for q in questions}
        ids = list(q_by_id.keys())
        batch_size = 100
        for i in range(0, len(ids), batch_size):
            batch = ids[i:i + batch_size]
            path = f"/questions/{';'.join(str(x) for x in batch)}/answers"
            try:
                payload = self._get(path, {
                    "site": site,
                    "filter": _DEFAULT_FILTER,
                    "pagesize": batch_size,
                })
            except httpx.HTTPStatusError as e:
                # Answer fetch is best-effort — never fail the whole stream.
                log.warning("stackex answer batch failed: %s", e)
                continue
            for a in payload.get("items") or []:
                q = q_by_id.get(a["question_id"])
                if not q:
                    continue
                yield _a_to_item(a, site, q)

    def __del__(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
