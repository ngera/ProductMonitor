# Commercial MVP Plan — Customer Feedback Monitor

**Version:** 0.1
**Status:** Active — supersedes scope of [DESIGN.md](DESIGN.md) and reframes [PLATFORM_DESIGN.md](PLATFORM_DESIGN.md)
**Sibling docs:** [REDDIT_APPROVAL_PLAN.md](REDDIT_APPROVAL_PLAN.md) (now non-commercial reference only — see §5)

## 1. The pivot (locked decisions)

Three decisions, locked, that reframe everything:

| Decision | Value | Implication |
|---|---|---|
| **Reddit scope** | Keep, budget for commercial tier | Existing [REDDIT_APPROVAL_PLAN.md](REDDIT_APPROVAL_PLAN.md) is non-commercial only. A new commercial-Reddit plan is needed; the non-commercial path stays as a fallback / dev-loop reference. |
| **Deployment model** | Hosted SaaS, **multi-tenant from day one** | Flips [PLATFORM_DESIGN.md §D3](PLATFORM_DESIGN.md). Phase 7 (true multi-tenant) becomes Phase 1. |
| **Customer timing** | Soon — 1 to 3 months | No more documentation sweeps. We pick an MVP scope and build. Per-source docs get written just-in-time per source as it's integrated. |

## 2. What changes from prior docs

| Prior doc | Status |
|---|---|
| [DESIGN.md](DESIGN.md) | Personal-Windows reference deployment only. Useful as a working spec for "one topic, one source, one local LLM" but not the platform spec. |
| [PLATFORM_DESIGN.md](PLATFORM_DESIGN.md) | Long-term shape still correct. **§D3 flips** to multi-tenant SaaS. Phase 7 becomes Phase 1. Phase order is reshuffled — see §8 below. |
| [REDDIT_APPROVAL_PLAN.md](REDDIT_APPROVAL_PLAN.md) | Non-commercial path only. Add a header banner: "Non-commercial reference; for commercial use see [REDDIT_COMMERCIAL_PLAN.md] (todo)." |
| Per-source docs sweep (HN/GitHub/etc.) | **Cancelled.** Each source gets a small `SOURCE_<name>.md` written as we integrate it, with commercial-use TOS notes inline. |

## 3. MVP scope (1–3 months)

**In scope — required for a paying customer to derive value:**

1. **Auth & multi-tenancy.** Customer signup, login, per-tenant data isolation. One tenant = one customer.
2. **Topic management.** Customer creates 1+ "topics" (e.g., "our product line", "competitor X"). Each topic has a taxonomy and a search query set.
3. **Two source plugins, day-one:**
   - **Hacker News** (Algolia API). Instant: free, no approval, commercial-friendly TOS.
   - **GitHub Issues** (via a GitHub App, not personal PAT). Instant: free, commercial-friendly.
   - Reddit added as soon as commercial agreement closes — not blocking.
4. **One LLM adapter** end-to-end behind the router abstraction. Hosted (Anthropic or OpenAI) — Foundry Local doesn't fit a Linux server cleanly.
5. **Pipeline orchestration** running on a schedule (daily for V1, weekly for free-tier customers). Same shape as the existing pipeline but multi-tenant.
6. **Dashboard** (admin UI per tenant): items, grouped issues, basic filters.
7. **Sample-snippet labeling UI** — minimum form (paste text, label, save) backed by `topic_examples`. Used for few-shot at first; eval gold later.
8. **Billing.** Stripe, one paid plan + a free trial. Per-tenant usage metering (items processed, LLM tokens).
9. **Privacy policy + ToS + DPA template.** Hosted at the marketing URL.
10. **Operational basics:** error tracking (Sentry), structured logs (one aggregator), uptime monitor.

**Explicitly out of MVP scope** — deferred to V1.5+:

- All sources beyond HN / GitHub / Reddit (Bluesky, Mastodon, YouTube, Stack Exchange, RSS, etc.).
- LLM router with multiple adapters. One LLM is enough; the *abstraction* is in place, the *plugins* are 1.
- Per-issue enrichment via web-search LLM.
- Embedding-based clustering (V2 of old design).
- Webhooks out to customer systems.
- Custom taxonomy editor — taxonomy edited as YAML via support for V1.
- Self-host packaging — hosted SaaS only.
- SOC 2 audit (start the controls and documentation; certification is a later track).
- Multi-region / multi-cloud.
- Per-issue resurfacing / cross-week issue identity (V1.5 of old design).

## 4. Multi-tenant architecture sketch

### 4.1 Data isolation model

**Choice: row-level isolation with a mandatory `tenant_id` on every row, enforced by Postgres RLS (Row-Level Security).**

- Single database, single schema; every business table has `tenant_id UUID NOT NULL`.
- RLS policies use `current_setting('app.tenant_id')` set per request.
- Application sets the session variable on every connection checkout.
- Tenants cannot see each others' data even if the application has a bug — RLS is a hard backstop.

Alternative considered: per-tenant schema (Postgres) — more isolation but harder migrations and connection pooling. Defer to V1.5 if any customer demands it.

### 4.2 Object storage

S3-compatible object store for raw JSONL and rendered reports. Path layout: `s3://{bucket}/tenants/{tenant_id}/raw/{source}/{date}/items.jsonl`. IAM policy scopes the SaaS service to the bucket; tenants never see S3 directly.

### 4.3 Auth

- Customer login: managed auth (Clerk, Supabase Auth, or WorkOS). Build-from-scratch deferred.
- API access: per-tenant API keys, scoped to that tenant only.
- Roles V1: `owner`, `member`. RBAC expansion deferred.

### 4.4 Pipeline orchestration

- Background workers (Celery, RQ, or a managed worker queue) process per-tenant runs.
- Scheduler enqueues `RunTopic(tenant_id, topic_id)` jobs on each tenant's cadence.
- A run is the atomic unit of billing for LLM tokens; usage attributed to the tenant via the orchestrator.

### 4.5 PII / deletion propagation

- Every source has a `deletion_propagation_supported` flag in its manifest.
- For Reddit: weekly delta sync re-fetches recent items; items that 404 or return "[deleted]" trigger `mark_deleted` across raw JSONL, warehouse, and rendered reports.
- For GitHub: same — closed/locked/deleted issues propagate.
- Customer-side deletion: each tenant's data is purgeable via a `DELETE /v1/tenants/{id}/data` endpoint that wipes Postgres rows and S3 prefix.

## 5. Reddit commercial access

The non-commercial path in [REDDIT_APPROVAL_PLAN.md](REDDIT_APPROVAL_PLAN.md) is **not usable** for a hosted SaaS. The commercial path:

1. **Entry point:** the **Reddit Data API enterprise / commercial sign-up form** (linked from the Reddit Developer Platform help page; if not visible, contact `apidetails@reddit.com`).
2. **Pricing:** post-2023 pricing was reported at $0.24 per 1,000 API calls for the developer tier; commercial agreements are negotiated. Expect **$200–$2,000+/month** depending on volume across tenants.
3. **Required:** business entity, signed commercial agreement, DPO contact for GDPR, named compliance contact.
4. **Volume:** estimate generously and add headroom — going over your committed QPM in the middle of a multi-tenant run gets the whole service rate-limited.
5. **Timeline:** 4–8 weeks from first contact to signed agreement is typical. Start now, in parallel with build.
6. **Operationally:** one shared Reddit OAuth client across all tenants. Per-tenant fairness is enforced by the orchestrator (queue depth limits, per-tenant QPM caps).

A separate `documents/REDDIT_COMMERCIAL_PLAN.md` should be written before signing — captures negotiated pricing, contract obligations, and what the integration looks like operationally. Not blocking the MVP build since HN + GitHub cover signal until Reddit closes.

## 6. Commercial-use TOS notes for the MVP sources

| Source | TOS posture for commercial SaaS | Notes |
|---|---|---|
| **HN (Algolia)** | Allowed | Algolia's HN search index is publicly available; no API key required. Attribute the source in reports. |
| **GitHub Issues** | Allowed via **GitHub App**, not personal PAT | A GitHub App identifies as the SaaS, supports per-tenant installation, and respects GitHub's commercial rate limits (15K req/hr per installation). Build a GitHub App not a PAT integration. |
| **Reddit** | Requires commercial agreement (§5) | Until signed, do not enable Reddit for commercial tenants. |

Each future source plugin gets a `commercial_use` field in its manifest:

```python
commercial_use_status: str  # "allowed" | "allowed_with_attribution" | "requires_paid_tier" | "prohibited"
```

The orchestrator refuses to schedule a source for a paying tenant if its status is `prohibited` and the tenant hasn't accepted a separate consent.

## 7. Tech stack proposals

| Layer | Proposed | Rationale | Open? |
|---|---|---|---|
| Cloud | **AWS** | Most enterprise customers expect it; Bedrock gives an LLM option. | Open — see §9 |
| Compute | **ECS Fargate** for web + workers | No K8s ops at 1 cluster. | Open |
| Database | **Aurora Postgres** (RLS-capable) | Managed. RLS works. | Open — could be Supabase / Neon / RDS |
| Object store | **S3** | Standard. | Locked |
| Queue | **SQS** + worker pool | Native AWS, cheap. | Open — could be Redis + RQ |
| Auth | **Clerk** | Fastest path to working signup/login/SSO/SAML. SOC 2 certified themselves. | Open — could be Supabase Auth, WorkOS, Auth0 |
| Billing | **Stripe** | Standard. | Locked |
| LLM | **Anthropic Claude** for `classify` (json_schema strict), **Haiku** for `relevance` | Strong structured-output support; latest models; capable. Adapter pattern leaves room for OpenAI / Bedrock fallbacks. | Open |
| Embed | **Cohere** or **OpenAI** embeddings | Hosted. | Deferred to V1.5 |
| Web framework | **FastAPI** + **HTMX** admin UI | Continues current Python stack. SPA later if needed. | Locked |
| Marketing site | **Astro** static + Vercel | Cheap, fast, separate from app domain. | Open |
| Observability | **Sentry** (errors) + **Datadog** or **Better Stack** (logs/metrics) | Standard. | Open |
| DNS / CDN | **Cloudflare** | Standard. | Locked |
| Secrets | **AWS Secrets Manager** | No `.env` files in prod. | Locked |
| CI/CD | **GitHub Actions** → ECS | Standard. | Open |

## 8. Build phases (1–3 month MVP)

Each phase is 2–3 weeks. Phases overlap where possible.

### Phase 1 — Foundation (weeks 1–2)

- Refactor existing code into the plugin shape from [PLATFORM_DESIGN.md §4–5](PLATFORM_DESIGN.md): `Source` ABC + manifest, `LLMAdapter` + router. (Lift current Reddit + Foundry Local code, but **don't ship them in MVP** — see Phase 2.)
- New `tenant_id` and `topic_id` columns on every business table.
- Postgres + RLS scaffold (locally via docker-compose first, then Aurora).
- Auth: Clerk integration. One bootstrap user.
- FastAPI app skeleton with `/v1/tenants`, `/v1/topics`, `/v1/runs`.

### Phase 2 — Pipeline on hosted infra, one source, one LLM (weeks 2–4)

- HN Algolia source plugin.
- Anthropic Claude adapter (relevance + classify) replacing Foundry Local for the hosted path. Local Foundry Local stays available behind a feature flag for dev / personal use.
- Pipeline runs end-to-end for one tenant, one topic. Writes to per-tenant Postgres rows and per-tenant S3 prefix.
- Static report rendered from `core/render/` writes to S3, served via signed URL.
- AWS deployment: ECS Fargate (web + worker), Aurora Postgres, SQS, Secrets Manager.

### Phase 3 — GitHub App + dashboard + snippet labeling (weeks 4–6)

- GitHub App registration; per-tenant installation flow.
- GitHub Issues source plugin using the App.
- Admin dashboard (HTMX): topics list, source list, items list, issues view, run history.
- Sample-snippet labeling form (`POST /v1/topics/{id}/examples`).
- Webhook subscription model: customer can configure a callback URL, fires on `run.completed`.

### Phase 4 — Billing + onboarding + ToS/Privacy (weeks 6–8)

- Stripe Billing integration; one plan + 14-day free trial.
- Usage metering: `llm_tokens_in/out`, `items_processed` aggregated to the tenant level, pushed to Stripe usage records.
- Privacy Policy, Terms of Service, DPA template hosted at marketing domain.
- Customer signup flow: signup → tenant created → first topic wizard → first run.
- Error tracking (Sentry), uptime monitoring, structured logs to Better Stack.

### Phase 5 — Reddit commercial path, when agreement closes (parallel)

- Started week 1 in parallel as a business-track item, not engineering.
- Engineering integration is ~1 week of work once the contract is signed.
- Reddit source plugin re-enabled for commercial tenants.

### What ships at the end of week 8

A paying customer can sign up at the marketing site, complete onboarding, create a topic monitoring HN + GitHub Issues for keywords they choose, see a weekly report in their dashboard, pay via Stripe. Reddit on as soon as the commercial agreement closes (likely week 6–10).

## 9. Blocking decisions before code starts

Five decisions blocking Phase 1 kickoff. The §7 "Open" items map roughly to these.

1. **Cloud:** AWS vs Azure vs GCP vs Fly.io / Render. AWS recommended; Azure if there's an existing relationship; Fly.io if speed-to-deploy beats enterprise polish.
2. **Hosted LLM:** Anthropic direct vs OpenAI direct vs Bedrock (Claude on AWS) vs Azure OpenAI. Bedrock and Azure OpenAI add enterprise-procurement appeal; direct APIs are simpler.
3. **Auth provider:** Clerk vs Supabase Auth vs WorkOS vs Auth0. Clerk recommended for fastest path; WorkOS if enterprise SSO is a near-term ask.
4. **Brand / domain.** Need a real product name and domain before signup pages, ToS, and Stripe products are set up.
5. **Where to host the source code remote (GitHub org / private vs public).** Push to git is still pending.

Two non-blocking but high-leverage decisions to make in the first two weeks:

6. **Reddit commercial process:** who from your side talks to Reddit, when? Engineering doesn't unblock this — it's a sales/legal track.
7. **Customer #1 design partner:** is there a specific company in mind? If yes, their topic/sources can drive Phase 2 prioritization.

## 10. What's NOT changing

- Pipeline shape (fetch → normalize → filter → relevance → classify → group → score → aggregate → render) stays.
- Source / LLM plugin contracts from [PLATFORM_DESIGN.md §4–5](PLATFORM_DESIGN.md) stay.
- Eval-first discipline from [DESIGN.md §7](DESIGN.md) stays — but the golden set becomes per-tenant per-topic, not global.
- Verbatim preservation, source attribution, completeness logging, deletion propagation as *first-class* — these get *harder* under multi-tenant, not less important.

---

*This doc is the contract for the next 8 weeks. PLATFORM_DESIGN.md is the contract for the 12-month direction. DESIGN.md is the personal reference deployment.*
