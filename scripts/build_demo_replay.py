"""Build the offline demo bundle (POST_V1 §4.12, ADR-0010).

Generates two artifacts consumed by `product-monitor demo`:

  data/demo/raw/hn/{DEMO_WEEK}/hn-demo-notion.jsonl
      Synthetic-but-plausible HN discussions about Notion. Authors scrubbed
      to placeholder handles; content is short paraphrase, not verbatim.

  data/demo/llm_replay.jsonl
      One record per (role, item) pair — the exact hash the replay adapter
      will compute for that item's rendered prompt, paired with a
      hand-written classify/relevance response.

Re-run this whenever the demo prompts, taxonomy, or item list change.

Prompt drift is detected the loud way: the hash key changes and the demo
raises `LLMError: replay miss ...` on the first affected item.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.config import set_current_product
from pipeline.extract import extract
from pipeline.llm import _prompt_key
from pipeline.product import load_product


DEMO_WEEK = "2026-W29"
RAW_DIR = ROOT / "data" / "demo" / "raw" / "hn" / DEMO_WEEK
REPLAY_PATH = ROOT / "data" / "demo" / "llm_replay.jsonl"


# ---------------------------------------------------------------------------
# Synthetic HN items about Notion. Kept short + paraphrased so we're not
# republishing real HN comments verbatim; still gives the demo a realistic
# spread of feature areas + content types.
# ---------------------------------------------------------------------------


ITEMS: list[dict] = [
    {
        "external_id": "demo-hn-1001",
        "created_at": "2026-07-14T09:12:00+00:00",
        "title": "Notion databases grind to a halt past ~10k rows",
        "body": (
            "Been on Notion for two years, love the block editor. But once any "
            "database crosses about ten thousand rows the whole page becomes "
            "sluggish. Filters take 5–10 seconds, sort spins one CPU core, and "
            "the mobile app just shows a loading state indefinitely. Anyone "
            "else running into a soft ceiling here?"
        ),
        "engagement": {"points": 214, "comment_count": 87},
        "author": "hn_user_1",
    },
    {
        "external_id": "demo-hn-1002",
        "created_at": "2026-07-14T15:44:00+00:00",
        "title": "Is Notion AI actually worth $10/month if I already pay for ChatGPT Plus?",
        "body": (
            "Trying to justify the Notion AI add-on. The in-page summaries "
            "and draft-writing are convenient, but the output quality feels a "
            "clear step behind GPT-4o. What workflows make this a keeper for "
            "you if you already pay for another assistant?"
        ),
        "engagement": {"points": 143, "comment_count": 62},
        "author": "hn_user_2",
    },
    {
        "external_id": "demo-hn-1003",
        "created_at": "2026-07-15T08:00:00+00:00",
        "title": "The block editor still spoils me for every other tool",
        "body": (
            "Every time I try to go back to plain Markdown files I miss "
            "Notion's block editor. Slash commands, drag-to-reorder, nested "
            "toggles — nothing else feels as fluid. Obsidian is close on the "
            "offline story but the editing UX is a step down for me."
        ),
        "engagement": {"points": 312, "comment_count": 118},
        "author": "hn_user_3",
    },
    {
        "external_id": "demo-hn-1004",
        "created_at": "2026-07-15T11:30:00+00:00",
        "title": "Offline mode still barely usable on mobile",
        "body": (
            "Traveled last week and tried to actually use Notion on the plane. "
            "Pages I'd opened on wifi were sometimes there, sometimes blank. "
            "Any linked database was gone entirely. This has been a known gap "
            "for years — what's the roadmap?"
        ),
        "engagement": {"points": 96, "comment_count": 54},
        "author": "hn_user_4",
    },
    {
        "external_id": "demo-hn-1005",
        "created_at": "2026-07-15T18:20:00+00:00",
        "title": "Formulas 2.0 finally makes complex rollups tolerable",
        "body": (
            "Rebuilt three of my most gnarly databases with the new formula "
            "language. Being able to write proper multi-line expressions with "
            "let-bindings makes maintenance so much easier. Small quality-of-"
            "life wins add up."
        ),
        "engagement": {"points": 174, "comment_count": 41},
        "author": "hn_user_5",
    },
    {
        "external_id": "demo-hn-1006",
        "created_at": "2026-07-16T07:05:00+00:00",
        "title": "Bug: linked database filters reset after page reload",
        "body": (
            "Steps: open page with a linked-database view, change the filter "
            "to 'Status is Doing', reload the page. Filter is back to default. "
            "Reproduces on Chrome + Firefox on my Mac, but not on the desktop "
            "app. Started ~last Tuesday."
        ),
        "engagement": {"points": 71, "comment_count": 23},
        "author": "hn_user_6",
    },
    {
        "external_id": "demo-hn-1007",
        "created_at": "2026-07-16T12:47:00+00:00",
        "title": "Enterprise pricing feels punitive for small teams growing into it",
        "body": (
            "We're eight people, hit a hard wall trying to enable SSO without "
            "moving to Enterprise. The per-seat jump is 3x. Understand the "
            "logic but the middle tier's feature set makes it feel like the "
            "product is nudging small teams toward a plan they'll only need "
            "one feature from."
        ),
        "engagement": {"points": 208, "comment_count": 96},
        "author": "hn_user_7",
    },
    {
        "external_id": "demo-hn-1008",
        "created_at": "2026-07-17T09:15:00+00:00",
        "title": "Notion Calendar makes me question why I ever used Google Calendar",
        "body": (
            "Been using the standalone Notion Calendar app for a month. Time-"
            "block dragging feels closer to Fantastical than to gcal. Being "
            "able to attach a Notion page to any event closes a loop I didn't "
            "know I wanted closed."
        ),
        "engagement": {"points": 261, "comment_count": 89},
        "author": "hn_user_8",
    },
    {
        "external_id": "demo-hn-1009",
        "created_at": "2026-07-17T14:02:00+00:00",
        "title": "Feature request: proper column-level permissions in databases",
        "body": (
            "We're using a database as a shared roadmap and want to expose "
            "some columns to the whole company but hide priority/estimate "
            "columns from non-editors. Currently the only option is to fork "
            "the database into two, which defeats the point."
        ),
        "engagement": {"points": 149, "comment_count": 44},
        "author": "hn_user_9",
    },
    {
        "external_id": "demo-hn-1010",
        "created_at": "2026-07-18T08:33:00+00:00",
        "title": "AI autofill is a productivity trap for anything factual",
        "body": (
            "Tried the AI autofill on a database of vendors, asking it to "
            "populate the 'headquarters' column. Half the cities were wrong "
            "with high confidence. Fine for brainstorming; dangerous when you "
            "assume it's grounded."
        ),
        "engagement": {"points": 187, "comment_count": 78},
        "author": "hn_user_10",
    },
    {
        "external_id": "demo-hn-1011",
        "created_at": "2026-07-18T16:41:00+00:00",
        "title": "Toggles inside callouts inside toggles — where does the block editor break?",
        "body": (
            "I've been stress-testing nesting depth in the block editor. Past "
            "about six levels deep, drag handles start showing up on the "
            "wrong block, and undo occasionally rewinds an edit two levels "
            "up. Anyone else pushing the nesting past what's comfortable?"
        ),
        "engagement": {"points": 84, "comment_count": 37},
        "author": "hn_user_11",
    },
    {
        "external_id": "demo-hn-1012",
        "created_at": "2026-07-19T09:58:00+00:00",
        "title": "Sync conflict UI is finally reasonable",
        "body": (
            "The redesigned conflict resolution dialog is a real improvement. "
            "Side-by-side diff, clear timestamps, no more silent losing-writer "
            "surprises. Small thing that's been a footgun for years, glad it's "
            "fixed."
        ),
        "engagement": {"points": 118, "comment_count": 22},
        "author": "hn_user_12",
    },
    {
        "external_id": "demo-hn-1013",
        "created_at": "2026-07-19T13:12:00+00:00",
        "title": "How do you keep AI-generated pages from cluttering search?",
        "body": (
            "Team's been leaning on Notion AI to draft weekly updates. Search "
            "results are now dominated by AI first-drafts nobody polished. "
            "Any workflow for tagging AI drafts so they can be excluded from "
            "search by default?"
        ),
        "engagement": {"points": 92, "comment_count": 51},
        "author": "hn_user_13",
    },
    {
        "external_id": "demo-hn-1014",
        "created_at": "2026-07-19T17:44:00+00:00",
        "title": "Bug: PDF export truncates long tables in databases",
        "body": (
            "Export a database view with 200+ rows to PDF, only the first "
            "page of rows makes it into the file. No warning, no error. "
            "Reproduces on Business plan on both my Mac and a colleague's "
            "Windows machine. Started after the June release."
        ),
        "engagement": {"points": 63, "comment_count": 19},
        "author": "hn_user_14",
    },
    {
        "external_id": "demo-hn-1015",
        "created_at": "2026-07-19T21:07:00+00:00",
        "title": "Notion is quietly becoming the best doc collaboration tool for small teams",
        "body": (
            "Two years ago I'd have said Google Docs was still ahead on pure "
            "prose collaboration. Between the block editor, real-time cursors, "
            "and inline comments that don't feel bolted on, I don't think that's "
            "true anymore for teams under fifty."
        ),
        "engagement": {"points": 231, "comment_count": 74},
        "author": "hn_user_15",
    },
]


# Hand-crafted classify outputs, one per item. Each is a valid instance of the
# CoreClassification + demo ProductExtras schema.
CLASSIFICATIONS: dict[str, dict] = {
    "demo-hn-1001": {
        "is_topic_relevant": True,
        "areas": ["databases", "sync_performance"],
        "content_types": ["bug_report", "feedback"],
        "sentiment": -0.5,
        "summary": "Large databases (>~10k rows) become slow and unusable across devices.",
        "confidence": 0.9,
        "user_context": "power user with multi-year Notion usage",
        "bug_severity": "high",
        "bug_is_regression": False,
        "bug_reproducibility": "always",
        "bug_repro_steps_quality": "partial",
        "bug_repro_steps": ["Open a Notion database with >10,000 rows",
                             "Apply a filter or sort",
                             "Observe multi-second lag; mobile app hangs"],
        "bug_preconditions": ["database size > 10k rows"],
        "request_specificity": None,
        "request_existing_workaround": None,
        "entities": [],
        "extras": {},
    },
    "demo-hn-1002": {
        "is_topic_relevant": True,
        "areas": ["ai_features", "pricing_plans"],
        "content_types": ["question", "comparison"],
        "sentiment": -0.1,
        "summary": "User weighing $10/mo Notion AI add-on against existing ChatGPT Plus subscription.",
        "confidence": 0.9,
        "user_context": "existing paying ChatGPT user considering Notion AI",
        "entities": [
            {"type": "software", "product": "ChatGPT",
             "role": "software_in_use", "confidence": 0.9, "verbatim": "ChatGPT Plus"},
        ],
        "extras": {},
    },
    "demo-hn-1003": {
        "is_topic_relevant": True,
        "areas": ["editor"],
        "content_types": ["praise", "comparison"],
        "sentiment": 0.7,
        "summary": "Strong preference for the Notion block editor over Markdown and Obsidian.",
        "confidence": 0.9,
        "user_context": "long-time user who has tried alternatives",
        "entities": [
            {"type": "software", "product": "Obsidian",
             "role": "software_in_use", "confidence": 0.9, "verbatim": "Obsidian"},
        ],
        "extras": {},
    },
    "demo-hn-1004": {
        "is_topic_relevant": True,
        "areas": ["sync_performance"],
        "content_types": ["bug_report", "rant"],
        "sentiment": -0.6,
        "summary": "Offline mode on mobile is unreliable — pages sometimes missing, linked DBs absent entirely.",
        "confidence": 0.85,
        "user_context": "mobile user, travel scenario",
        "bug_severity": "medium",
        "bug_is_regression": False,
        "bug_reproducibility": "intermittent",
        "bug_repro_steps_quality": "partial",
        "bug_repro_steps": ["Open pages on wifi",
                             "Go offline",
                             "Attempt to view pages / linked databases"],
        "bug_preconditions": ["mobile app", "no network"],
        "entities": [],
        "extras": {},
    },
    "demo-hn-1005": {
        "is_topic_relevant": True,
        "areas": ["databases"],
        "content_types": ["praise", "feedback"],
        "sentiment": 0.6,
        "summary": "Formulas 2.0 makes complex database rollups significantly easier to maintain.",
        "confidence": 0.9,
        "user_context": "database power user",
        "entities": [],
        "extras": {},
    },
    "demo-hn-1006": {
        "is_topic_relevant": True,
        "areas": ["databases"],
        "content_types": ["bug_report"],
        "sentiment": -0.4,
        "summary": "Linked database filter resets after page reload — browser only, not desktop app.",
        "confidence": 0.95,
        "user_context": "browser + desktop app user on Mac",
        "bug_severity": "medium",
        "bug_is_regression": True,
        "bug_reproducibility": "always",
        "bug_repro_steps_quality": "detailed",
        "bug_repro_steps": ["Open a page containing a linked-database view",
                             "Change the filter (e.g., 'Status is Doing')",
                             "Reload the page",
                             "Observe filter reset to default"],
        "bug_preconditions": ["browser (Chrome or Firefox) on macOS"],
        "entities": [],
        "extras": {},
    },
    "demo-hn-1007": {
        "is_topic_relevant": True,
        "areas": ["pricing_plans"],
        "content_types": ["feedback", "rant"],
        "sentiment": -0.6,
        "summary": "Small teams needing SSO forced into Enterprise; per-seat jump feels disproportionate.",
        "confidence": 0.9,
        "user_context": "small-team admin (8 seats)",
        "entities": [],
        "extras": {},
    },
    "demo-hn-1008": {
        "is_topic_relevant": True,
        "areas": ["editor"],
        "content_types": ["praise", "comparison"],
        "sentiment": 0.7,
        "summary": "Notion Calendar viewed as a strong alternative to Google Calendar / Fantastical.",
        "confidence": 0.9,
        "user_context": "calendar power user",
        "entities": [],
        "extras": {},
    },
    "demo-hn-1009": {
        "is_topic_relevant": True,
        "areas": ["databases"],
        "content_types": ["feature_request"],
        "sentiment": -0.2,
        "summary": "Request for column-level permissions on database views to enable partial sharing.",
        "confidence": 0.95,
        "user_context": "team using database as shared roadmap",
        "request_specificity": "specific",
        "request_existing_workaround": True,
        "entities": [],
        "extras": {},
    },
    "demo-hn-1010": {
        "is_topic_relevant": True,
        "areas": ["ai_features", "databases"],
        "content_types": ["feedback", "rant"],
        "sentiment": -0.6,
        "summary": "AI autofill is confidently wrong on factual data; unsafe outside brainstorming.",
        "confidence": 0.9,
        "user_context": "user with a factual-data use case",
        "entities": [],
        "extras": {},
    },
    "demo-hn-1011": {
        "is_topic_relevant": True,
        "areas": ["editor"],
        "content_types": ["bug_report", "question"],
        "sentiment": -0.3,
        "summary": "Deeply nested block editor exhibits drag-handle and undo bugs past ~6 levels.",
        "confidence": 0.85,
        "user_context": "user stress-testing block editor limits",
        "bug_severity": "low",
        "bug_is_regression": False,
        "bug_reproducibility": "intermittent",
        "bug_repro_steps_quality": "partial",
        "bug_repro_steps": ["Nest blocks ~6+ levels deep",
                             "Attempt to drag; observe drag handle misalignment",
                             "Undo an edit; observe undo affecting the wrong nesting level"],
        "bug_preconditions": ["nesting depth > ~6 levels"],
        "entities": [],
        "extras": {},
    },
    "demo-hn-1012": {
        "is_topic_relevant": True,
        "areas": ["sync_performance"],
        "content_types": ["praise"],
        "sentiment": 0.6,
        "summary": "Redesigned sync-conflict resolution UI is a clear improvement.",
        "confidence": 0.9,
        "user_context": "long-time user familiar with the older UI",
        "entities": [],
        "extras": {},
    },
    "demo-hn-1013": {
        "is_topic_relevant": True,
        "areas": ["ai_features"],
        "content_types": ["question", "feedback"],
        "sentiment": -0.2,
        "summary": "Team wants a way to exclude unpolished AI drafts from workspace search.",
        "confidence": 0.9,
        "user_context": "team using Notion AI for drafts",
        "entities": [],
        "extras": {},
    },
    "demo-hn-1014": {
        "is_topic_relevant": True,
        "areas": ["databases"],
        "content_types": ["bug_report"],
        "sentiment": -0.5,
        "summary": "PDF export truncates database tables past ~200 rows silently.",
        "confidence": 0.95,
        "user_context": "cross-platform Business-plan users",
        "bug_severity": "medium",
        "bug_is_regression": True,
        "bug_reproducibility": "always",
        "bug_repro_steps_quality": "detailed",
        "bug_repro_steps": ["Open a database view with 200+ rows",
                             "Export as PDF",
                             "Observe only the first page of rows in the output"],
        "bug_preconditions": ["Business plan", "database with > ~200 rows"],
        "entities": [],
        "extras": {},
    },
    "demo-hn-1015": {
        "is_topic_relevant": True,
        "areas": ["editor"],
        "content_types": ["praise", "comparison"],
        "sentiment": 0.7,
        "summary": "Notion has overtaken Google Docs for small-team doc collaboration.",
        "confidence": 0.9,
        "user_context": "long-time cross-tool user; team < 50 people",
        "entities": [],
        "extras": {},
    },
}


def _raw_record(item: dict) -> dict:
    """Shape one demo item as the JSONL fetch writes."""
    return {
        "source": "hn",
        "source_display_name": "Hacker News",
        "external_id": item["external_id"],
        "url": f"https://news.ycombinator.com/item?id={item['external_id']}",
        "parent_external_id": None,
        "author": item["author"],
        "created_at": item["created_at"],
        "title": item["title"],
        "body": item["body"],
        "engagement": item["engagement"],
        "raw": {"tags": ["story"], "story_id": None, "is_comment": False},
    }


def _classify_item_shape(item: dict) -> dict:
    """Item dict shape used by classify._build_prompt (matches the DB row)."""
    return {
        "id": f"hn:{item['external_id']}",
        "title": item["title"],
        "body": item["body"],
        "source": "hn",
        "source_display_name": "Hacker News",
        "engagement_json": json.dumps(item["engagement"]),
        "raw": {},
    }


def _relevance_response(item: dict, cls: dict) -> str:
    """Hand-generated relevance JSON. All demo items are Notion-relevant."""
    return json.dumps({
        "relevant": True,
        "confidence": 0.95,
    })


def _classify_response(cls: dict) -> str:
    """Serialize the hand-authored classification into the schema JSON."""
    return json.dumps(cls, ensure_ascii=False)


def _write_raw() -> Path:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / "hn-demo-notion.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for it in ITEMS:
            f.write(json.dumps(_raw_record(it), ensure_ascii=False) + "\n")
    return path


def _write_replay() -> Path:
    from pipeline import classify as _cls, relevance as _rel

    load_product.cache_clear()  # be idempotent when re-run
    product = load_product("demo")
    set_current_product(product)

    records: list[dict] = []
    for item in ITEMS:
        cls = CLASSIFICATIONS[item["external_id"]]

        # --- relevance ---
        rel_system, rel_user = _rel._render_prompt(item["title"], item["body"])
        rel_key = _prompt_key("relevance", rel_system, rel_user)
        records.append({
            "key": rel_key,
            "role": "relevance",
            "stage": "relevance",
            "item_id": f"hn:{item['external_id']}",
            "content": _relevance_response(item, cls),
            "usage": {"prompt_tokens": 220, "completion_tokens": 18},
        })

        # --- classify ---
        item_row = _classify_item_shape(item)
        regex_res = extract(f"{item['title']}\n{item['body']}")
        cls_system, cls_user = _cls._build_prompt(item_row, regex_res)
        cls_key = _prompt_key("classify", cls_system, cls_user)
        records.append({
            "key": cls_key,
            "role": "classify",
            "stage": "classify",
            "item_id": f"hn:{item['external_id']}",
            "content": _classify_response(cls),
            "usage": {"prompt_tokens": 1450, "completion_tokens": 260,
                       "cached_input_tokens": 900},
        })

    REPLAY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with REPLAY_PATH.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return REPLAY_PATH


def main() -> int:
    raw_path = _write_raw()
    print(f"[demo-bundle] wrote {len(ITEMS)} raw items to {raw_path}")
    replay_path = _write_replay()
    print(f"[demo-bundle] wrote {2 * len(ITEMS)} replay records to {replay_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
