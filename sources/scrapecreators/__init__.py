"""ScrapeCreators source plugins (POST_V1_PLAN §4.9).

Three platform plugins share one HTTP client + one API key:
  - scrapecreators_reddit
  - scrapecreators_x
  - scrapecreators_tiktok

Env vars:
  SCRAPECREATORS_API_KEY   Required. Single key across all three platforms.
  SCRAPECREATORS_MOCK      Optional. Set to "1" to use fixture-based responses
                           from tests/fixtures/scrapecreators/ instead of the
                           live API (see mock.py). Zero credits burned.

Per-run credit cap comes from `fetching.scrapecreators_max_credits_per_run`
in config/app.yaml (default 200). 402 from the API is a hard halt for
that source; no retries burning credits.

Feature-flagged by `features.scrapecreators_enabled` — plugins are
discovered but do not fetch when the flag is off.
"""
