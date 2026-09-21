# ProductMonitor

**Monitor user feedback and media coverage about your product — on your
machine.**

ProductMonitor is a local-first tool for product owners, PMs, and small
teams who want a clear weekly answer to: *“What are people saying about
us?”* It watches public sources (forums, Reddit, Hacker News, app
stores, RSS/press, GitHub, …), filters noise with a configurable LLM
(or skips LLM entirely), and writes a **static HTML digest** you open
in the browser.

No SaaS signup. No shared warehouse. Data lives under `data/<product>/`
on the computer that runs it. MIT licensed — see [LICENSE](LICENSE).

---

## What you get in ~10 minutes

1. **User feedback** — community posts, issues, reviews, discussions.
2. **Media coverage** — press and blog RSS from a built-in publication
   catalog (Wired, The Verge, TechCrunch, …) plus your own feeds.
3. **A weekly digest** — themed sections, ranked items, mandatory
   source attribution — not a raw dump of search results.
4. **Your choice of LLM** — Anthropic / OpenAI / Gemini / … or local
   Ollama; or fetch-only with no model at all.

```bash
uvx product-monitor demo    # offline demo report, no keys
uvx product-monitor ui      # open http://127.0.0.1:8766
```

From a checkout or Docker: see [documents/SETUP.md](documents/SETUP.md).

---

## How it works (one screen)

```
Sources  →  Fetch  →  Filter  →  Relevance  →  Classify  →  Digest HTML
 (plugins)   (raw)    (rules)     (LLM)         (LLM)      (static file)
```

You configure a **product** once (name, themes, sources, models). Each
**run** covers a time window (typically a week). Re-runs can skip fetch
and reuse raw data when you are tuning prompts.

Two LLM roles keep cost in check:

| | Pipeline | Assistant |
|---|---|---|
| **Job** | Relevance + classify at volume | Wizard drafting, headlines |
| **Where** | Per-product routing | One global connection |
| **Tip** | Prefer cheap/local models | Optional stronger hosted model |

---

## Who it is for

- PMs who want a Monday digest without pasting URLs into ChatGPT
- Founders watching early community + press signal
- Teams that **cannot** send raw feedback to a third-party analytics SaaS

It is **not** a multi-tenant cloud product, a real-time social firehose,
or a replacement for your support inbox.

---

## Docs map

| Doc | Read when |
|---|---|
| [Architecture](documents/ARCHITECTURE.md) | You want the system diagram and data layout |
| [Setup and onboarding](documents/SETUP.md) | Install, wizard, adding sources & LLM providers |
| [Key design decisions](documents/DESIGN_DECISIONS.md) | Cost, performance, and “why built this way” |

---

## Trust and privacy (short version)

- Secrets only in `.env` (never in committed YAML)
- UI listens on localhost by default
- Every item in the digest carries attribution back to the source
- Hosted LLMs are opt-in; local models need no API key

---

## License

MIT — [LICENSE](LICENSE).
