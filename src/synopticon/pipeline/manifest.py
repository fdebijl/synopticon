"""Model manifest handling: load, sha256 verification, path resolution.

The manifest (``<models_dir>/manifest.json``) is written by
``scripts/download_models.py`` and maps a model key to::

    {"file": "...", "sha256": "...", "source_url": "...", "license": "..."}

The pipeline refuses to load any model whose on-disk sha256 does not match
the manifest entry. Locally-exported models (AdaFace/MagFace) get their hash
recorded on first registration.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

MANIFEST_NAME = "manifest.json"

# Canonical set of model keys the pipeline needs before it can run extract,
# mapped to the on-disk filename each one is stored under. This is the single
# source of truth for "is the install ready?" — it does NOT depend on what the
# manifest happens to list, because a partial download registers only the
# models it fetched while extraction needs all five. Filenames mirror the
# ``file`` entries in ``scripts/download_models.py``'s KNOWN_MODELS registry; a
# consistency guard in that script asserts the two never drift.
REQUIRED_MODELS: dict[str, str] = {
    "scrfd_10g_bnkps": "scrfd_10g_bnkps.onnx",
    "yolov8l-face": "yolov8l-face.onnx",
    "glintr100": "glintr100.onnx",
    "adaface_ir101_webface12m": "adaface_ir101_webface12m.onnx",
    "magface_iresnet100": "magface_iresnet100.onnx",
}


def missing_models(models_dir: Path | str) -> list[str]:
    """Return the REQUIRED_MODELS keys whose weight file is absent on disk.

    Pure ``pathlib`` — checks file presence only (not manifest registration,
    not sha256). An empty list means every required model is present, i.e. the
    install is ready for extraction.
    """
    models_dir = Path(models_dir)
    return [
        key
        for key, filename in REQUIRED_MODELS.items()
        if not (models_dir / filename).is_file()
    ]


class ModelIntegrityError(RuntimeError):
    """Raised when a model file's sha256 does not match the manifest."""


def manifest_path(models_dir: Path | str) -> Path:
    return Path(models_dir) / MANIFEST_NAME


def manifest_bytes(models_dir: Path | str) -> bytes:
    """Raw manifest content, b'' when absent (used for pipeline_version)."""
    path = manifest_path(models_dir)
    return path.read_bytes() if path.is_file() else b""


def load_manifest(models_dir: Path | str) -> dict[str, dict[str, Any]]:
    path = manifest_path(models_dir)
    if not path.is_file():
        return {}
    return json.loads(path.read_text())


def save_manifest(models_dir: Path | str, manifest: dict[str, dict[str, Any]]) -> None:
    path = manifest_path(models_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def sha256_file(path: Path | str, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def resolve_model(models_dir: Path | str, key: str, verify: bool = True) -> Path:
    """Return the verified path for a manifest key.

    Raises KeyError (not in manifest), FileNotFoundError (file gone) or
    ModelIntegrityError (sha256 mismatch — the pipeline must refuse these).
    """
    models_dir = Path(models_dir)
    manifest = load_manifest(models_dir)
    if key not in manifest:
        raise KeyError(
            f"Model '{key}' not in {manifest_path(models_dir)}; "
            f"run scripts/download_models.py first."
        )
    entry = manifest[key]
    path = models_dir / entry["file"]
    if not path.is_file():
        raise FileNotFoundError(f"Model file missing: {path} (manifest key '{key}')")
    if verify:
        actual = sha256_file(path)
        expected = entry.get("sha256")
        if expected and actual != expected:
            raise ModelIntegrityError(
                f"sha256 mismatch for '{key}': manifest={expected} actual={actual}. "
                f"Refusing to load {path}; re-download or re-export the model."
            )
    return path


def register_model(
    models_dir: Path | str,
    key: str,
    file: str,
    source_url: str,
    license: str,
    sha256: str | None = None,
) -> dict[str, Any]:
    """Record a model in the manifest; hash is computed on first registration.

    If the key already exists with a different sha256, raises
    ModelIntegrityError instead of silently re-pinning.
    """
    models_dir = Path(models_dir)
    path = models_dir / file
    actual = sha256 or sha256_file(path)
    manifest = load_manifest(models_dir)
    existing = manifest.get(key)
    if existing and existing.get("sha256") and existing["sha256"] != actual:
        raise ModelIntegrityError(
            f"'{key}' already registered with sha256={existing['sha256']} but file "
            f"has {actual}. Delete the manifest entry explicitly if this is intended."
        )
    entry = {"file": file, "sha256": actual, "source_url": source_url, "license": license}
    manifest[key] = entry
    save_manifest(models_dir, manifest)
    return entry
