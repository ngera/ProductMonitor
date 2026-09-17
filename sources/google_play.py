"""Google Play Store reviews via the Play Developer API (reviews.list).

IMPORTANT limits (encoded in manifest help):
  - Only apps YOU own in Play Console — no competitor coverage.
  - Only reviews created or modified in roughly the last 7 days — no backfill.
  - Product schedule should be at least weekly or reviews fall out of the window.

Auth: service-account JSON in .env as GOOGLE_PLAY_SERVICE_ACCOUNT_JSON
(minified JSON string). The account must be invited in Play Console with
"Reply to reviews" permission.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

import httpx

from pipeline import http as _retry_http
from pipeline.models import RawItem
from sources.base import FetchStats, FieldSpec, Source, SourceCursor, SourceManifest

log = logging.getLogger(__name__)

_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
_API_BASE = "https://androidpublisher.googleapis.com/androidpublisher/v3"
_USER_AGENT = "product-monitor/0.1"


MANIFEST = SourceManifest(
    plugin_id="google_play",
    display_name="Google Play Reviews",
    version="0.1.0",
    docs_url="https://developers.google.com/android-publisher/reply-to-reviews",
    help=(
        "Play Developer API reviews.list — YOUR apps only (service account "
        "must be invited in Play Console). Returns reviews from roughly the "
        "last 7 days only; no historical backfill. Run at least weekly or "
        "reviews silently fall out of the window. Cannot monitor competitor "
        "apps (unlike Apple App Store)."
    ),
    connection_fields=[
        FieldSpec(
            name="GOOGLE_PLAY_SERVICE_ACCOUNT_JSON",
            label="Service account JSON",
            type="secret",
            required=True,
            help="Minified service-account JSON from Google Cloud. Play Console "
                 "must invite this account with Reply to reviews permission.",
        ),
    ],
    stream_fields=[
        FieldSpec(name="name", label="Stream name", type="text", required=True,
                  placeholder="myapp-android", help="Internal label for cursor / dedup."),
        FieldSpec(name="package_name", label="Package name", type="text", required=True,
                  placeholder="com.example.app",
                  help="Android applicationId you own in Play Console."),
        FieldSpec(name="translation_language", label="Translation language", type="text",
                  default="", help="Optional ISO language code for translated review text."),
        FieldSpec(name="max_results", label="Max results per page", type="number", default=100,
                  help="API max is 100; paginate with token until exhausted."),
    ],
    identifier_field="package_name",
    source_category="custom_source",
    content_types=["user_feedback"],
)


def _parse_google_ts(raw: str) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _load_credentials():
    raw = (os.environ.get("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON") or "").strip()
    if not raw:
        raise RuntimeError(
            "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON not set. Create a Google Cloud "
            "service account, invite it in Play Console (Reply to reviews), "
            "and paste the minified JSON into .env."
        )
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON is not valid JSON."
        ) from e
    try:
        from google.oauth2 import service_account
        import google.auth.transport.requests as google_requests
    except ImportError as e:
        raise RuntimeError(
            "google-auth is required for google_play source. "
            "Install with: pip install google-auth"
        ) from e
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=[_SCOPE],
    )
    creds.refresh(google_requests.Request())
    return creds


class GooglePlaySource(Source):
    name = "google_play"

    def __init__(self) -> None:
        self._creds = _load_credentials()
        self._client = httpx.Client(
            headers={"User-Agent": _USER_AGENT},
            timeout=httpx.Timeout(30.0),
        )

    def fetch_since(
        self, cursor: SourceCursor, config: dict[str, Any], stats: FetchStats
    ) -> Iterator[RawItem]:
        package = (config.get("package_name") or "").strip()
        if not package:
            raise ValueError("google_play stream config missing required 'package_name'")

        max_results = min(100, max(1, int(config.get("max_results") or 100)))
        translation = (config.get("translation_language") or "").strip() or None
        floor: float = float(cursor.cursor_ts or 0)
        newest_seen = floor

        token: Optional[str] = None
        while True:
            params: dict[str, Any] = {"maxResults": max_results}
            if translation:
                params["translationLanguage"] = translation
            if token:
                params["token"] = token

            url = f"{_API_BASE}/applications/{package}/reviews"
            resp = _retry_http.request_with_retry(
                lambda: self._client.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {self._creds.token}"},
                ),
                source_id="google_play",
            )
            resp.raise_for_status()
            data = resp.json()

            for review in data.get("reviews") or []:
                item = _review_to_item(review, package)
                if item is None:
                    continue
                ts = item.created_at.timestamp()
                if ts <= floor:
                    continue
                if ts > newest_seen:
                    newest_seen = ts
                yield item

            token = (data.get("tokenPagination") or {}).get("nextPageToken")
            if not token:
                break

        if newest_seen > floor:
            cursor.cursor_ts = newest_seen

    def __del__(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


def _review_to_item(review: dict[str, Any], package: str) -> Optional[RawItem]:
    review_id = review.get("reviewId")
    if not review_id:
        return None
    comments = review.get("comments") or []
    user_comment = None
    for c in comments:
        if "userComment" in c:
            user_comment = c["userComment"]
            break
    if not user_comment:
        return None

    text = (user_comment.get("text") or "").strip()
    rating = int(user_comment.get("starRating") or 0)
    author = (user_comment.get("reviewerLanguage") or "")  # no public author name
    last_mod = user_comment.get("lastModified") or {}
    seconds = last_mod.get("seconds")
    if seconds is not None:
        created = datetime.fromtimestamp(int(seconds), tz=timezone.utc)
    else:
        created = datetime.now(timezone.utc)

    # Play has no stable public per-review URL; link to the app's Play Store page.
    url = f"https://play.google.com/store/apps/details?id={package}"

    return RawItem(
        source="google_play",
        source_display_name=f"Google Play ({package})",
        external_id=f"{package}:{review_id}",
        url=url,
        parent_external_id=None,
        author=author or None,
        created_at=created,
        title=None,
        body=text,
        content_type="user_feedback",
        engagement={"rating": rating, "thumbs_up": int(user_comment.get("thumbsUpCount") or 0)},
        raw={"review_id": review_id, "package_name": package},
    )
