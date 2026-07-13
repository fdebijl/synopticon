"""Embedding ensemble: run the available subset of ArcFace/AdaFace/MagFace.

The ensemble reads ``<models_dir>/manifest.json`` and instantiates whichever of
the three embedders are present and integrity-verified. A missing manifest
entry or missing file degrades gracefully (warn + drop that model); a sha256
mismatch (:class:`ModelIntegrityError`) is *not* swallowed — the pipeline must
refuse a tampered/corrupt model. Construction raises only when zero embedders
are available.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from ...config import InferenceConfig
from ..manifest import ModelIntegrityError, load_manifest
from .adaface import AdaFaceEmbedder
from .arcface import ArcFaceEmbedder
from .magface import MagFaceEmbedder

log = logging.getLogger(__name__)

# Logical embedder name -> (embedder class, manifest key). Sorted-name order of
# this mapping also defines the concat order used for the fused vector.
_REGISTRY: list[tuple[str, type, str]] = [
    (AdaFaceEmbedder.name, AdaFaceEmbedder, AdaFaceEmbedder.MANIFEST_KEY),
    (ArcFaceEmbedder.name, ArcFaceEmbedder, ArcFaceEmbedder.MANIFEST_KEY),
    (MagFaceEmbedder.name, MagFaceEmbedder, MagFaceEmbedder.MANIFEST_KEY),
]

MAGFACE_NAME = MagFaceEmbedder.name


class EmbeddingEnsemble:
    def __init__(self, models_dir: Path | str, inference: InferenceConfig | None = None):
        self.models_dir = Path(models_dir)
        self.inference = inference or InferenceConfig()
        manifest = load_manifest(self.models_dir)

        self.embedders: dict[str, object] = {}
        self.model_versions: dict[str, str | None] = {}
        for name, cls, key in _REGISTRY:
            entry = manifest.get(key)
            if entry is None:
                log.warning(
                    "Embedder '%s' unavailable: manifest key '%s' missing in %s "
                    "(run scripts/download_models.py / export scripts). Degrading.",
                    name,
                    key,
                    self.models_dir,
                )
                continue
            try:
                self.embedders[name] = cls.from_manifest(self.models_dir, self.inference)
            except ModelIntegrityError:
                raise  # never silently drop a corrupt/tampered model
            except (KeyError, FileNotFoundError) as exc:
                log.warning("Embedder '%s' unavailable: %s. Degrading.", name, exc)
                continue
            self.model_versions[name] = entry.get("sha256")

        if not self.embedders:
            raise RuntimeError(
                f"No embedding models available in {self.models_dir}. "
                f"Run scripts/download_models.py and the export scripts first."
            )
        log.info("Embedding ensemble ready: %s", sorted(self.embedders))

    @property
    def available_models(self) -> list[str]:
        return sorted(self.embedders)

    @property
    def dims(self) -> dict[str, int]:
        return {name: emb.dim for name, emb in self.embedders.items()}

    def _iter_batches(self, n: int):
        step = max(1, self.inference.batch_size)
        for start in range(0, n, step):
            yield start, min(start + step, n)

    def run(self, crops: np.ndarray) -> tuple[dict[str, np.ndarray], np.ndarray | None]:
        """Embed a batch of aligned crops with every available embedder.

        Returns ``(embeddings, magface_norms)`` where ``embeddings`` maps each
        available model name to an (N, dim) L2-normalized float32 array, and
        ``magface_norms`` is the (N,) pre-normalization MagFace magnitude array
        (quality signal) or ``None`` when MagFace is unavailable.
        """
        arr = np.asarray(crops)
        if arr.ndim == 3:
            arr = arr[None, ...]
        n = arr.shape[0]

        out: dict[str, list[np.ndarray]] = {name: [] for name in self.embedders}
        mag_norms: list[np.ndarray] = []
        has_mag = MAGFACE_NAME in self.embedders

        if n == 0:
            empty = {name: np.zeros((0, emb.dim), np.float32) for name, emb in self.embedders.items()}
            return empty, (np.zeros((0,), np.float32) if has_mag else None)

        for lo, hi in self._iter_batches(n):
            chunk = arr[lo:hi]
            for name, emb in self.embedders.items():
                if name == MAGFACE_NAME:
                    vecs, norms = emb.embed_with_norm(chunk)
                    mag_norms.append(norms)
                else:
                    vecs = emb.embed(chunk)
                out[name].append(vecs)

        embeddings = {name: np.concatenate(parts) for name, parts in out.items()}
        magface_norms = np.concatenate(mag_norms) if has_mag else None
        return embeddings, magface_norms


def fuse(embeddings: dict[str, np.ndarray]) -> np.ndarray:
    """L2-normalized concat of per-model vecs in sorted model-name order.

    Cosine of the fused vector is proportional to the mean of the per-model
    cosines, which is the fusion used at cluster time.
    """
    from .base import l2_normalize

    names = sorted(embeddings)
    concat = np.concatenate([np.asarray(embeddings[n], dtype=np.float32) for n in names], axis=1)
    return l2_normalize(concat)
