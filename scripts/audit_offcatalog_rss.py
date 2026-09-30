"""Report every rss feed_url in a product sources.yaml that isn't in the
master publications catalog (`config/media_sources.yaml`).

Read-only. Prints a per-product report. Nothing is modified — the operator
decides per-URL whether to add it to the catalog, retire it from the
product, or leave it alone.

Usage:
    python -m scripts.audit_offcatalog_rss
    python -m scripts.audit_offcatalog_rss --product windows-os
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from pipeline import media_sources as _media
from pipeline.config import project_root


def _iter_streams(source: dict):
    """Yield each stream dict on a source, base + _extra_streams handled."""
    if not isinstance(source, dict) or source.get("type") != "rss":
        return
    if source.get("feed_url") or source.get("url"):
        yield source
    for st in source.get("streams") or []:
        if isinstance(st, dict):
            yield st
    for st in (source.get("stream_config") or {}).get("_extra_streams") or []:
        if isinstance(st, dict):
            yield st


def audit(only_product: str | None = None) -> int:
    catalog_urls = {m["feed_url"] for m in _media.load()}
    products_dir = project_root() / "products"
    if not products_dir.is_dir():
        print("No products/ directory.")
        return 0

    total_offcatalog = 0
    for pdir in sorted(products_dir.iterdir()):
        if not pdir.is_dir():
            continue
        if only_product and pdir.name != only_product:
            continue
        sources_yaml = pdir / "sources.yaml"
        if not sources_yaml.exists():
            continue
        try:
            data = yaml.safe_load(sources_yaml.read_text(encoding="utf-8")) or {}
        except Exception as e:
            print(f"[{pdir.name}] YAML read error: {e}", file=sys.stderr)
            continue
        offcatalog: list[tuple[str, str]] = []
        for src in data.get("sources") or []:
            if not isinstance(src, dict) or src.get("type") != "rss":
                continue
            src_id = src.get("id") or "<no id>"
            for st in _iter_streams(src):
                url = st.get("feed_url") or st.get("url") or ""
                if url and url not in catalog_urls:
                    offcatalog.append((src_id, url))
        if offcatalog:
            print(f"\n== {pdir.name} ==")
            for src_id, url in offcatalog:
                print(f"  {src_id}: {url}")
            total_offcatalog += len(offcatalog)

    print(f"\nTotal off-catalog rss feeds: {total_offcatalog}")
    print("Master catalog: config/media_sources.yaml "
          f"({len(catalog_urls)} entries)")
    return total_offcatalog


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--product", help="only audit this product id")
    args = ap.parse_args()
    n = audit(only_product=args.product)
    return 0 if n == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
