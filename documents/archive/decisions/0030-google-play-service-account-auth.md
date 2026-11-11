# ADR-0030: Google Play reviews via service-account auth

- **Status:** Accepted
- **Date:** 2026-09-17
- **Area:** infra / data

## Context

ProductMonitor monitors Apple App Store reviews via a keyless public RSS
feed. Google has no public equivalent — the Play Developer API
`reviews.list` returns reviews only for apps the operator owns, via a
Google Cloud service account invited in Play Console. The API also
returns only reviews created or modified within roughly the last seven
days.

We need a first-party `google_play` source plugin for Android parity
(DoorDash and other consumer apps skew Android-heavy).

## Options considered

1. **Scrape Play Store pages** — violates ToS, brittle HTML, no stable
   IDs. Rejected.
2. **Official API + service account (chosen)** — honest limits, stable
   IDs, matches how Play Console operators already manage reviews.
3. **Defer Android entirely** — leaves a documented asymmetry vs iOS.
   Rejected for Tier 1 scope.

## Decision

Add `sources/google_play.py` using `google-auth` for OAuth2
service-account tokens against the Android Publisher API. Store the
service-account JSON in `.env` as `GOOGLE_PLAY_SERVICE_ACCOUNT_JSON`
(minified string). Manifest help text states: owned apps only, ~7-day
window, weekly cadence minimum.

Add `google-auth==2.36.0` to `requirements.txt` (lightweight; avoids
pulling the full `google-api-python-client`).

## Rationale

Service-account auth is the only supported programmatic path for
owned-app reviews. `google-auth` is the minimal dependency for token
refresh; HTTP calls use existing `httpx`.

## Consequences

- Operators must create GCP credentials and invite the account in Play
  Console before the source works.
- No competitor Android review monitoring (unlike Apple).
- Historical backfill is out of scope; continuous polling is required.

## Revisit if

Google ships a public review feed comparable to Apple's RSS, or extends
the API window beyond seven days with a documented stable contract.
