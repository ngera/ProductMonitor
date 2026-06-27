"""Topic-specific extension schema for the Windows topic.

Composed onto CoreClassification at runtime by the orchestrator. Holds the
Windows-version-context fields that used to live directly on Classification
(pipeline/models.py before Phase 0).

The class is named `WindowsExtras` and is referenced from topics/windows/topic.yaml
as `extras_module: extras` + `extras_class: WindowsExtras`. To add a new topic,
write your own `extras.py` with a Pydantic class containing whatever fields
your topic needs and point your topic.yaml at it.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class WindowsExtras(BaseModel):
    # Windows major version family
    windows_major: str = "unknown"          # win10 | win11 | win_server | unknown

    # Marketing feature update, when explicit (24H2, 25H2, etc.)
    windows_feature_update: Optional[str] = None

    # Numeric build number, when explicit (e.g. "26100.4061")
    windows_build: Optional[str] = None

    # Insider channel, when applicable
    windows_channel: Optional[str] = None   # stable | release_preview | beta | dev | canary

    # How we know the version: explicit (user said), inferred (we guessed), unknown
    windows_version_confidence: str = "unknown"
