"""Apple App Store customer-reviews connector via the public iTunes RSS/JSON feed.

No auth required. Uses the legacy but still-live customer-reviews endpoint:

    https://itunes.apple.com/{country}/rss/customerreviews/page={n}/id={app_id}/sortBy=mostRecent/json

Limits are hard-coded by Apple:
  - ~50 reviews per page
  - Pages 1..10 available (so ~500 recent reviews per country per app)
  - Only most-recent sort works predictably

Reviews are country-specific — a US review isn't in the GB feed. To monitor
multiple regions, add each country you care about to `countries`.

Stream config:

    streams:
      - name: myapp-us
        app_id: 284882215           # required. Apple's numeric app id.
        countries: [us, gb, jp]     # default: [us]
        max_pages: 10               # 1..10, hard-capped at 10
        min_rating: 0               # 0 = keep all; 3 = drop 3-star and up (low complaint value)
        max_rating: 5               # 5 = keep all; 2 = only 1-2 star (rants)

Cursor is the epoch-second `updated` timestamp of the newest review seen
across all countries. Reviews older than the cursor are skipped without
re-emit.

Gotcha: on page 1 the *first* entry is the app metadata (label = "iTunes
Customer Reviews"), not a real review. We detect and skip it via the
`im:rating` field's absence.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

import httpx

from pipeline import http as _retry_http

from pipeline.models import RawItem
from sources.base import FetchStats, FieldSpec, Source, SourceCursor, SourceManifest

MANIFEST = SourceManifest(
    plugin_id="apple_appstore",
    display_name="Apple App Store",
    version="0.1.0",
    docs_url="https://apps.apple.com",
    help=(
        "Customer reviews via the public iTunes RSS/JSON feed. No auth. "
        "One stream per app you want to monitor; add multiple countries "
        "to the same stream to get regional coverage. Filter by rating "
        "if you only care about complaints (1-2 stars) vs. all reviews."
    ),
    connection_fields=[],
    stream_fields=[
        FieldSpec(name="name", label="Stream name", type="text", required=True,
                  placeholder="netflix-us", help="Internal label for cursor / dedup."),
        FieldSpec(name="app_id", label="Apple app id", type="text", required=True,
                  placeholder="363590051",
                  help="The numeric id from the App Store URL (apps.apple.com/us/app/…/id{THIS}). Copy just the digits."),
        FieldSpec(name="countries", label="Countries (comma list)", type="csv", default="us",
                  help="ISO country codes: us, gb, ca, de, fr, jp, kr, in, ... Each is a separate ~500-review pool."),
        FieldSpec(name="max_pages", label="Max pages per country", type="number", default=10,
                  help="Apple caps at 10 pages (~500 reviews). Lower this if you only care about the latest N reviews."),
        FieldSpec(name="min_rating", label="Minimum rating", type="number", default=0,
                  help="0 = keep all. Set to 3 to drop 3-5 star reviews (keep only complaints)."),
        FieldSpec(name="max_rating", label="Maximum rating", type="number", default=5,
                  help="5 = keep all. Set to 2 for a 1-2 star rants-only stream."),
    ],
    identifier_field="app_id",
    source_category="custom_source",
    content_types=["user_feedback"],
)

log = logging.getLogger(__name__)

_BASE_URL = "https://itunes.apple.com"
_USER_AGENT = "product-monitor/0.1"
_MAX_PAGES = 10  # Apple hard cap; requesting page 11+ returns empty.


def _parse_apple_ts(raw: str) -> Optional[datetime]:
    """Apple's `updated` field looks like '2026-07-08T14:22:31-07:00'.
    Return a UTC-aware datetime, or None on parse failure."""
    if not raw:
        return None
    try:
        # fromisoformat handles the -07:00 offset natively on 3.11+
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _pluck(entry: dict[str, Any], key: str) -> Optional[str]:
    """iTunes RSS-JSON wraps every leaf as {'label': 'value'} or {'attributes': {...}}.
    Return the label if present, else None."""
    v = entry.get(key)
    if isinstance(v, dict):
        return v.get("label")
    return v


def _is_review_entry(entry: dict[str, Any]) -> bool:
    """The first entry on page 1 is the app metadata blob (no `im:rating`).
    Real reviews always have im:rating."""
    return "im:rating" in entry


def _review_to_item(entry: dict[str, Any], app_id: str, country: str) -> Optional[RawItem]:
    """Map one review entry to a RawItem. Return None if the entry is
    malformed enough that we can't produce a stable id."""
    review_id = _pluck(entry, "id")
    if not review_id:
        return None
    try:
        rating = int(_pluck(entry, "im:rating") or 0)
    except (TypeError, ValueError):
        rating = 0
    version = _pluck(entry, "im:version") or ""
    title = _pluck(entry, "title") or ""
    body = _pluck(entry, "content") or ""

    author_dict = entry.get("author") or {}
    author_name = None
    author_uri = None
    if isinstance(author_dict, dict):
        author_name = _pluck(author_dict, "name")
        author_uri = _pluck(author_dict, "uri")

    updated_raw = _pluck(entry, "updated") or ""
    updated = _parse_apple_ts(updated_raw) or datetime.now(timezone.utc)

    # Deep link to the review. Apple doesn't publish a stable per-review URL,
    # so we link to the app page in that country + the review id as a fragment.
    link = f"https://apps.apple.com/{country}/app/id{app_id}?see-all=reviews#review-{review_id}"

    return RawItem(
        source="apple_appstore",
        source_display_name=f"Apple App Store ({country.upper()})",
        external_id=f"{country}:{review_id}",
        url=link,
        parent_external_id=None,
        author=author_name,
        created_at=updated,
        title=title,
        body=body,
        content_type="user_feedback",
        engagement={
            "rating": rating,           # 1..5 stars, primary signal
            "app_version": version,     # which app version the review was left on
        },
        raw={
            "country": country,
            "app_id": app_id,
            "author_uri": author_uri,
        },
    )


class AppleAppStoreSource(Source):
    name = "apple_appstore"

    def __init__(self) -> None:
        self._client = httpx.Client(
            base_url=_BASE_URL,
            headers={"User-Agent": _USER_AGENT},
            timeout=httpx.Timeout(20.0),
        )

    def _fetch_page(self, country: str, app_id: str, page: int) -> dict[str, Any]:
        """One page of reviews. Apple's URL scheme is positional (page + id in path)."""
        path = f"/{country}/rss/customerreviews/page={page}/id={app_id}/sortBy=mostRecent/json"
        resp = _retry_http.request_with_retry(
            lambda p=path: self._client.get(p),
            source_id="apple_appstore",
        )
        resp.raise_for_status()
        return resp.json()

    def fetch_since(
        self, cursor: SourceCursor, config: dict[str, Any], stats: FetchStats
    ) -> Iterator[RawItem]:
        app_id = str(config.get("app_id") or "").strip()
        if not app_id:
            raise ValueError("apple_appstore stream config missing required 'app_id'")

        countries = config.get("countries") or ["us"]
        if isinstance(countries, str):
            countries = [c.strip() for c in countries.split(",") if c.strip()]
        countries = [c.lower() for c in countries]

        max_pages = max(1, min(int(config.get("max_pages", _MAX_PAGES)), _MAX_PAGES))
        min_rating = int(config.get("min_rating", 0))
        max_rating = int(config.get("max_rating", 5))
        sleep_between_pages = float(config.get("sleep_between_pages_seconds", 0.5))
        display_name = config.get("name") or f"apple-{app_id}"

        floor: float = float(cursor.cursor_ts or 0)
        newest_seen = floor

        for country in countries:
            reviews_this_country = 0
            for page in range(1, max_pages + 1):
                try:
                    payload = self._fetch_page(country, app_id, page)
                except httpx.HTTPStatusError as e:
                    # 404 typically means the app has no reviews in this country
                    # (or app_id is wrong). Don't crash the whole stream.
                    log.warning(
                        "apple_appstore fetch failed country=%s app_id=%s page=%d: %s",
                        country, app_id, page, e,
                    )
                    break

                feed = payload.get("feed") or {}
                entries = feed.get("entry") or []
                # `entry` can be a single object rather than a list when only
                # one review lives on that page. Normalize.
                if isinstance(entries, dict):
                    entries = [entries]
                if not entries:
                    break

                page_had_new = False
                for entry in entries:
                    if not _is_review_entry(entry):
                        continue  # page-1 app-metadata blob

                    item = _review_to_item(entry, app_id, country)
                    if item is None:
                        continue

                    ts = item.created_at.timestamp()
                    if ts <= floor:
                        # Reviews are sorted mostRecent — once we hit the
                        # cursor floor, everything after is old too.
                        break

                    rating = int(item.engagement.get("rating") or 0)
                    if rating < min_rating or rating > max_rating:
                        continue

                    if ts > newest_seen:
                        newest_seen = ts
                    reviews_this_country += 1
                    page_had_new = True
                    yield item
                else:
                    # For-else: only runs if the loop wasn't broken out of.
                    # Meaning we processed the whole page and could have more.
                    if sleep_between_pages:
                        time.sleep(sleep_between_pages)
                    continue
                # break inner loop propagated out here
                break

            log.info(
                "apple_appstore country=%s reviews=%d floor_ts=%s",
                country, reviews_this_country, floor,
            )

        if newest_seen > floor:
            cursor.cursor_ts = newest_seen

    def __del__(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
