"""Jinja rendering helpers for the digest.

Kept intentionally small — the real content shape lives in sections.py and
the templates. Template files live at report_templates/digest_*.html.j2 +
_digest_base.html.j2.
"""

from __future__ import annotations

from jinja2 import Environment, FileSystemLoader, select_autoescape

from pipeline.config import project_root


TEMPLATE_DIR = project_root() / "report_templates"


def env() -> Environment:
    """Autoescape-on Jinja environment reading from report_templates/."""
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2", "html.j2"], default=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )
