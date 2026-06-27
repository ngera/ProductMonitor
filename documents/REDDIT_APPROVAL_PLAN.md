# Reddit Data API — Approval Plan

> **Status:** Canonical Reddit path for this project. V1 is a local-run open-source tool ([LOCAL_V1_PLAN.md](LOCAL_V1_PLAN.md)); each user runs Reddit under their own personal non-commercial credentials registered per this plan. (The earlier "superseded for commercial" banner was removed when the cloud-SaaS direction was reversed.)

Reddit's official approval pages (`support.reddithelp.com`) and the non-commercial sign-up form are not directly fetchable from automated tools, so the process below is grounded in public Reddit Help articles plus third-party reporting on the 2025 policy change. Field names may have shifted since this was written — open the live form and adapt; the substance is what matters.

## Two separate developer offerings (do not confuse them)

| | Devvit (developers.reddit.com) | Reddit Data API |
|---|---|---|
| **Runs where** | Inside Reddit (sandboxed JS/TS, Reddit-hosted) | Your machine, over HTTPS |
| **Purpose** | Interactive posts, custom mod tools, in-feed games | Reading/writing Reddit data from external programs |
| **Auth** | Reddit account + Devvit CLI | OAuth2 `client_id` / `client_secret` |
| **Fits this project?** | No | Yes |

This project targets the **Reddit Data API** via PRAW (see [sources/reddit.py](../sources/reddit.py)).

## Step 0 — prerequisites on your Reddit account

Apps tied to weak accounts are commonly auto-rejected. Before applying:

1. Log in to Reddit with the account you'll register the app under.
2. Verify the email on the account.
3. Enable 2FA (Account settings → Safety & Privacy → Two-Factor Authentication).
4. Confirm the account is >30 days old and has non-zero karma. If brand-new, post/comment a few times in r/test or general subs first.
5. Make sure the account is not suspended or shadowbanned on any of the target subs in [config/sources.yaml](../config/sources.yaml).

## Step 1 — register the script app at prefs/apps

This gives you `client_id` and `client_secret` immediately, before approval. They won't work for live calls until approval is granted, but you need them for the application form.

1. Go to `https://www.reddit.com/prefs/apps`
2. Click **"create another app…"** at the bottom
3. Fill in:
   - **name**: `customer-feedback-monitor`
   - **type**: select **script**
   - **description**: `Personal weekly read-only aggregation of public consumer-product feedback from Reddit (starting with Microsoft Windows) for local analysis. Non-commercial.`
   - **about url**: blank, or a GitHub repo link if/when public
   - **redirect uri**: `http://localhost:8080` (unused by script apps but required by the form)
4. Click **create app**. Record:
   - `client_id` — the short string directly under the app name (under "personal use script")
   - `client_secret` — the longer "secret" field
   - The Reddit username you registered under
5. Drop these into `.env` (do not commit):
   ```
   REDDIT_CLIENT_ID=...
   REDDIT_CLIENT_SECRET=...
   REDDIT_USER_AGENT=customer-feedback-monitor:0.1 (by /u/<your_handle>)
   ```

The User-Agent format above matters: Reddit throttles or 429s non-conforming UAs. Fix the fallback at [sources/reddit.py:38](../sources/reddit.py#L38) to match.

## Step 2 — submit the non-commercial Data API access request

Entry point: the **"Sign up for non-commercial access"** link on Reddit's [Developer Platform & Accessing Reddit Data](https://support.reddithelp.com/hc/en-us/articles/14945211791892-Developer-Platform-Accessing-Reddit-Data) help article. If Reddit reshuffles the link, follow the "Data API Wiki" link from the same page and look for the non-commercial sign-up form.

Pre-staged answers (paste, edit username/handle):

**Use-case category**: Non-commercial / personal / research.

**Project name**: Customer Feedback Monitor.

**One-line description**: A locally-run weekly tool that aggregates public Reddit posts about a chosen consumer product (starting with Microsoft Windows) into a static HTML report for personal review.

**Detailed description**:
> Personal, single-user project. Each week I run a Python script on my own machine that reads public posts and comments from a small list of subreddits focused on the product I'm tracking — initially Microsoft Windows (r/Windows11, r/Windows, r/WindowsHelp, r/sysadmin). It classifies each item with a locally-hosted small language model (Phi-4-mini via Foundry Local — runs on my own GPU, no third-party LLM API), groups items by relevant identifiers (e.g. for Windows: KB number, named hardware/driver), and renders a static HTML report I read in my browser. No data is republished, shared, resold, or made available to anyone else. No commercial intent. No model training on Reddit data. The subreddit list is small and explicitly declared; expanding to track a different product would mean reconfiguring the subreddit list, not increasing volume.

**App type / OAuth flow**: Script app, application-only (read-only) OAuth. Registered at prefs/apps as `customer-feedback-monitor` (paste your `client_id`).

**Subreddits accessed**: List from [config/sources.yaml](../config/sources.yaml) — currently `Windows11`, `Windows`, `WindowsHelp`, `sysadmin`. State that the list is small and Windows-related and that no subreddit is accessed beyond what's listed.

**Endpoints used**: `/r/{sub}/new`, `/r/{sub}/top?t=week`, `/r/{sub}/controversial?t=week`, `/r/{sub}/comments/{article}`, `/r/{sub}/search` (with `restrict_sr=true`).

**Expected request volume**: ~100–300 requests per weekly run, well under the 100 QPM ceiling.

**Data storage**: Local DuckDB warehouse + raw JSONL on a single Windows machine. Never uploaded, never shared.

**Data retention**: Indefinite locally for personal trend analysis; not redistributed. If a user deletes content on Reddit, I will not re-publish or share it. (Call this out explicitly — Reddit weights deletion compliance.)

**Will you display Reddit content publicly?**: No. Static HTML reports are read only by me on my own machine.

**Will you train AI/ML models on Reddit data?**: No. The local LLM (Phi-4-mini) is used only at inference time to classify and summarize individual items in my local report. No fine-tuning, no data export, no model artifacts shared.

**Commercial intent**: None. No revenue, no users besides me, no SaaS, no API resale.

**Compliance attestations** (checkboxes):
- Will comply with the **Reddit Data API Terms**
- Will comply with the **Developer Terms**
- Will comply with the **User Agreement**
- Will respect user deletions

## Step 3 — wait

Reported turnaround as of the 2025 policy change: **2–4 weeks**. Reddit may email follow-up questions — answer same-day; silence usually re-queues you to the back.

Common rejection reasons to pre-empt:
- Vague purpose ("research") with no concrete artifact — name the *report* as the output and mention it's HTML-on-disk, not a service.
- Sounding commercial (avoid "platform", "users", "customers", "dashboard for the team"). Stay first-person singular.
- Volume estimates that look like scraping (don't say "all of r/Windows"; say "the new / top-of-week / controversial-of-week listings, plus comment trees for matched posts, ~100–300 calls/week").
- New / low-karma account.

## Step 4 — after approval

1. Reddit emails confirmation. The same `client_id` / `client_secret` from Step 1 become live — no new credentials are issued. Nothing to swap in `.env`.
2. First call: smoke-test from a Python shell:
   ```python
   import praw, os
   r = praw.Reddit(client_id=os.environ["REDDIT_CLIENT_ID"],
                   client_secret=os.environ["REDDIT_CLIENT_SECRET"],
                   user_agent=os.environ["REDDIT_USER_AGENT"])
   r.read_only = True
   print(next(r.subreddit("Windows11").new(limit=1)).title)
   ```
   A 401 here means the app isn't approved yet or the User-Agent is malformed. A title means you're cleared.
3. Then run the existing connector at [sources/reddit.py](../sources/reddit.py).

## Step 5 — what to do while you wait

Approval is the long pole. Don't block on it:

- Build the eval golden set ([DESIGN.md §7.1](DESIGN.md#L623)) — V1 critical path, only needs hand-pasted Reddit URLs, no API access.
- Wire up Foundry Local and verify guided decoding works.
- Implement deterministic-grouping and rendering against fixture JSONL.

When the approval email lands, the pipeline is already exercised end-to-end on fixtures and the only new variable is real fetch.

## Sources

- [Developer Platform & Accessing Reddit Data – Reddit Help](https://support.reddithelp.com/hc/en-us/articles/14945211791892-Developer-Platform-Accessing-Reddit-Data)
- [Reddit Data API Wiki – Reddit Help](https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki)
- [Reddit's 2025 API Crackdown: Pre-Approval Now Required (ReplyDaddy)](https://replydaddy.com/blog/reddit-api-pre-approval-2025-personal-projects-crackdown)
- [How to Get Reddit API Key — Step-by-Step (Data365)](https://data365.co/blog/how-to-get-reddit-api-key)
- [Reddit API Documentation: Complete Developer Guide 2026 (Zernio)](https://zernio.com/blog/reddit-api-documentation)
