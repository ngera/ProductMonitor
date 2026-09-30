"""Tests for URL → snippet content fetch + simplified URL create flow."""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient


@pytest.fixture
def products_dir(tmp_path, monkeypatch):
    d = tmp_path / "products"
    d.mkdir()
    monkeypatch.setattr("pipeline.product.PRODUCTS_DIR", d)
    monkeypatch.setattr("pipeline.features.PRODUCTS_DIR", d)
    monkeypatch.setattr("webui.app.PRODUCTS_DIR", d)
    from pipeline import product as _p, features as _f
    _p.clear_cache(); _f.clear_cache()
    return d


@pytest.fixture
def client():
    from webui.app import app
    return TestClient(app)


def test_fetch_reddit_json(monkeypatch):
    from pipeline import snippet_fetch as sf

    sample = [{
        "data": {
            "children": [{
                "data": {
                    "title": "Help with auth",
                    "selftext": "RLS is blocking inserts",
                    "subreddit": "Supabase",
                }
            }]
        }
    }]

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return sample

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, url):
            assert ".json" in url
            return _Resp()

    monkeypatch.setattr(sf.httpx, "Client", _Client)
    out = sf.fetch_snippet_content(
        "https://www.reddit.com/r/Supabase/comments/abc123/help_with_auth/"
    )
    assert out.ok
    assert out.title == "Help with auth"
    assert "RLS" in out.body
    assert out.source_display_name == "r/Supabase"


def test_fetch_hn(monkeypatch):
    from pipeline import snippet_fetch as sf

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"title": "Show HN: widget", "text": "Hello <b>world</b>", "url": None}

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, url):
            assert "firebaseio.com" in url
            return _Resp()

    monkeypatch.setattr(sf.httpx, "Client", _Client)
    out = sf.fetch_snippet_content("https://news.ycombinator.com/item?id=12345")
    assert out.ok
    assert out.title == "Show HN: widget"
    assert "Hello" in out.body
    assert "<b>" not in out.body
    assert out.source_display_name == "Hacker News"


def test_fetch_rejects_empty_url():
    from pipeline.snippet_fetch import fetch_snippet_content
    out = fetch_snippet_content("")
    assert not out.ok
    assert "required" in out.error.lower()


def test_fetch_endpoint_and_url_create(client, products_dir, monkeypatch):
    from pipeline import product as product_mod
    product_mod.scaffold_product("acme", "Acme", "A test product")

    from pipeline.snippet_fetch import FetchedSnippet
    monkeypatch.setattr(
        "pipeline.snippet_fetch.fetch_snippet_content",
        lambda url, product_id=None: FetchedSnippet(
            ok=True, url=url, title="Fetched title",
            body="Fetched body text about the product",
            source_display_name="r/acme",
        ),
    )

    resp = client.post(
        "/products/acme/snippets/fetch",
        json={"url": "https://www.reddit.com/r/acme/comments/xyz/hi/"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["title"] == "Fetched title"
    assert "Fetched body" in data["body"]

    create = client.post(
        "/products/acme/snippets",
        data={
            "mode": "url",
            "polarity": "positive_example",
            "source_url": "https://www.reddit.com/r/acme/comments/xyz/hi/",
            "title": "Fetched title",
            "body": "Fetched body text about the product",
            "holdout_eval": "on",
        },
        follow_redirects=False,
    )
    assert create.status_code == 303
    assert "/products/acme/snippets" in create.headers["location"]

    examples = list((products_dir / "acme" / "examples" / "positive").glob("*.yaml"))
    assert len(examples) == 1
    blob = yaml.safe_load(examples[0].read_text(encoding="utf-8"))
    assert blob["source_url"].endswith("/hi/")
    assert blob["title"] == "Fetched title"
    assert blob["body"].startswith("Fetched body")
    assert blob["polarity"] == "positive_example"
    assert blob["holdout_eval"] is True
    assert blob["labels"]["is_topic_relevant"] is True


def test_url_mode_form_is_simplified(client, products_dir):
    from pipeline import product as product_mod
    product_mod.scaffold_product("acme", "Acme", "d")
    resp = client.get("/products/acme/snippets/new?mode=url")
    assert resp.status_code == 200
    assert "Fetch content" in resp.text
    assert 'id="snippet-preview"' in resp.text
    assert 'name="areas"' not in resp.text
    assert "Labels — how this should be classified" not in resp.text


def test_text_mode_form_is_simplified(client, products_dir):
    from pipeline import product as product_mod
    product_mod.scaffold_product("acme", "Acme", "d")
    resp = client.get("/products/acme/snippets/new?mode=text")
    assert resp.status_code == 200
    assert 'name="classification"' in resp.text
    assert "Bug / Issue" in resp.text
    assert "Feature" in resp.text
    assert "General context about product" in resp.text
    assert "Labels — how this should be classified" not in resp.text
    assert 'name="areas"' not in resp.text
    assert 'name="holdout_eval"' not in resp.text
    assert 'name="source_url"' not in resp.text


def test_text_mode_create_writes_classification(client, products_dir):
    from pipeline import product as product_mod
    product_mod.scaffold_product("acme", "Acme", "A test product")
    create = client.post(
        "/products/acme/snippets",
        data={
            "mode": "text",
            "polarity": "positive_example",
            "title": "Dashboard won't load",
            "body": "After login the dashboard spins forever.",
            "classification": "bug_report",
        },
        follow_redirects=False,
    )
    assert create.status_code == 303
    examples = list((products_dir / "acme" / "examples" / "positive").glob("*.yaml"))
    assert len(examples) == 1
    blob = yaml.safe_load(examples[0].read_text(encoding="utf-8"))
    assert blob["title"] == "Dashboard won't load"
    assert blob["labels"]["content_types"] == ["bug_report"]
    assert blob["labels"]["is_topic_relevant"] is True


def test_edit_form_matches_url_create(client, products_dir):
    from pipeline import product as product_mod
    from pipeline.snippets import Snippet, POSITIVE, save_snippet
    product_mod.scaffold_product("acme", "Acme", "d")
    save_snippet(
        products_dir / "acme",
        Snippet(
            id="sample",
            polarity=POSITIVE,
            source_url="https://www.reddit.com/r/acme/comments/abc/hi/",
            title="Hi",
            body="Body text",
            labels={"is_topic_relevant": True, "content_types": ["bug_report"]},
        ),
    )
    from webui.app import clear_cache
    clear_cache()
    resp = client.get("/products/acme/snippets/sample")
    assert resp.status_code == 200
    assert "Fetch content" in resp.text or "Re-fetch content" in resp.text
    assert 'id="snippet-preview"' in resp.text
    assert "Labels — how this should be classified" not in resp.text
    assert 'name="areas"' not in resp.text
    assert 'name="holdout_eval"' not in resp.text
    assert "Hi" in resp.text
    assert "Body text" in resp.text
