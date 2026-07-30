"""``pipeline_version``: the hash that gates re-extraction.

A deliberate *leaf* module — ``hashlib``/``json`` plus :mod:`.manifest`, nothing
else. It lives apart from :mod:`.runner` because the web process wants the
version string on the dashboard's hot path (``/api/stats``) and must never drag
in the extraction stack to get it: importing ``runner`` pulls numpy + cv2 (and,
with a cold page cache, seconds of paging their shared objects in) purely to
sha256 two small byte strings. ``runner`` re-exports this, so existing import
sites are unchanged.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..config import Settings
from .manifest import manifest_bytes


def pipeline_version(settings: Settings, models_dir: Path | str) -> str:
    """Short hash of the model manifest bytes + canonical detection config.

    Changing any detection threshold or swapping a model invalidates prior
    extractions, so the work queue picks those photos up again.
    """
    manifest = manifest_bytes(models_dir)
    det = json.dumps(
        settings.detection.model_dump(), sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(manifest + det.encode("utf-8")).hexdigest()
    return digest[:12]
