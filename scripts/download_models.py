#!/usr/bin/env python3
"""Download + verify Synopticon's pinned models into a --models-dir volume.

Run standalone (not via the Typer CLI)::

    uv run python scripts/download_models.py --models-dir /models

What it does per model key:

* Detectors/embedders bundled in insightface's ``antelopev2.zip``
  (``scrfd_10g_bnkps`` + ``glintr100``) are downloaded once (the zip is cached in
  models_dir) and the required members extracted.
* ``yolov8l-face`` has no dependable direct-ONNX release asset (AGPL-3.0) and is
  treated as OPTIONAL: instructions for a manual ``yolo export`` are printed; if
  the .onnx already exists it is registered.
* ``adaface_ir101_webface12m`` / ``magface_iresnet100`` are produced locally by
  ``scripts/export_*_onnx.py``; they are registered iff the .onnx already exists,
  else an instruction is printed.

sha256 policy: a model with a pinned hash is verified and refused on mismatch. A
model whose hash is not yet pinned (weights we cannot fetch here) is refused
unless ``--allow-record-hash`` is given, in which case the computed hash is
recorded (with a warning) so the maintainer can pin it afterwards.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

# Import the package's manifest helpers so the pipeline and this script agree on
# the on-disk contract and sha256 semantics.
try:
    from synopticon.pipeline import manifest as mf
except ImportError:  # pragma: no cover - allow running from a source checkout
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from synopticon.pipeline import manifest as mf

ANTELOPEV2_URL = (
    "https://github.com/deepinsight/insightface/releases/download/v0.7/antelopev2.zip"
)

# key -> spec. `sha256=None` means "not pinned yet" (needs --allow-record-hash).
KNOWN_MODELS: dict[str, dict] = {
    "scrfd_10g_bnkps": {
        "file": "scrfd_10g_bnkps.onnx",
        "sha256": "5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91",
        "source_url": ANTELOPEV2_URL,
        "license": "insightface antelopev2; weights for research use — verify terms",
        "archive": "antelopev2.zip",
        "member": "antelopev2/scrfd_10g_bnkps.onnx",
    },
    "glintr100": {
        "file": "glintr100.onnx",
        "sha256": "4ab1d6435d639628a6f3e5008dd4f929edf4c4124b1a7169e1048f9fef534cdf",
        "source_url": ANTELOPEV2_URL,
        "license": "insightface antelopev2 (Glint360K); weights for research use",
        "archive": "antelopev2.zip",
        "member": "antelopev2/glintr100.onnx",
    },
    "yolov8l-face": {
        "file": "yolov8l-face.onnx",
        "sha256": None,
        "source_url": "https://github.com/derronqi/yolov8-face",
        "license": "AGPL-3.0 (derronqi/yolov8-face)",
        "optional": True,
        "export_hint": (
            "No dependable direct ONNX asset. Obtain yolov8l-face.pt from "
            "https://github.com/derronqi/yolov8-face and export:\n"
            "    yolo export model=yolov8l-face.pt format=onnx opset=12\n"
            "then place yolov8l-face.onnx in --models-dir and re-run with "
            "--allow-record-hash."
        ),
    },
    "adaface_ir101_webface12m": {
        "file": "adaface_ir101_webface12m.onnx",
        "sha256": None,
        "source_url": "https://github.com/mk-minchul/AdaFace",
        "license": "MIT code; WebFace12M weights research-use (not redistributed)",
        "local_export": True,
        "export_hint": (
            "Export from the official checkpoint:\n"
            "    uv run --extra export python scripts/export_adaface_onnx.py "
            "--checkpoint adaface_ir101_webface12m.ckpt "
            "--out <models-dir>/adaface_ir101_webface12m.onnx"
        ),
    },
    "magface_iresnet100": {
        "file": "magface_iresnet100.onnx",
        "sha256": None,
        "source_url": "https://github.com/IrvingMeng/MagFace",
        "license": "Apache-2.0 code; iResNet100 weights research-use (not redistributed)",
        "local_export": True,
        "export_hint": (
            "Export from the official checkpoint:\n"
            "    uv run --extra export python scripts/export_magface_onnx.py "
            "--checkpoint magface_iresnet100.pth "
            "--out <models-dir>/magface_iresnet100.onnx"
        ),
    },
}


def _download(url: str, dest: Path) -> None:
    """Stream a URL to dest atomically."""
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkstemp(dir=dest.parent, suffix=".part")[1])
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=None) as resp:
            resp.raise_for_status()
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    fh.write(chunk)
        tmp.replace(dest)
    finally:
        if tmp.exists():
            tmp.unlink()


def _ensure_archive(models_dir: Path, url: str, archive_name: str) -> Path:
    archive = models_dir / archive_name
    if archive.is_file():
        print(f"  using cached {archive.name}")
        return archive
    print(f"  downloading {archive.name} ...")
    _download(url, archive)
    return archive


def _extract_member(archive: Path, member: str, dest: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        target = member if member in names else next(
            (n for n in names if n.endswith(Path(member).name)), None
        )
        if target is None:
            raise FileNotFoundError(f"'{member}' not found in {archive.name}: {names}")
        with zf.open(target) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)


def _register(models_dir: Path, key: str, spec: dict, allow_record_hash: bool) -> None:
    path = models_dir / spec["file"]
    actual = mf.sha256_file(path)
    pinned = spec.get("sha256")
    if pinned:
        if actual != pinned:
            raise mf.ModelIntegrityError(
                f"sha256 mismatch for '{key}': pinned={pinned} actual={actual}"
            )
    elif not allow_record_hash:
        raise SystemExit(
            f"'{key}' has no pinned sha256. Computed {actual}.\n"
            f"Pin it in KNOWN_MODELS, or re-run with --allow-record-hash to record it."
        )
    else:
        print(f"  WARNING: recording unpinned sha256 for '{key}': {actual}")
    mf.register_model(
        models_dir, key, spec["file"], spec["source_url"], spec["license"], sha256=actual
    )
    print(f"  registered '{key}' -> {spec['file']}")


def _handle(models_dir: Path, key: str, spec: dict, allow_record_hash: bool) -> None:
    print(f"[{key}]")
    dest = models_dir / spec["file"]

    if spec.get("local_export"):
        if dest.is_file():
            _register(models_dir, key, spec, allow_record_hash)
        else:
            print(f"  not present (local export). {spec['export_hint']}")
        return

    if spec.get("optional") and not spec.get("archive"):
        if dest.is_file():
            _register(models_dir, key, spec, allow_record_hash)
        else:
            print(f"  optional, not present. {spec['export_hint']}")
        return

    if not dest.is_file():
        if "archive" in spec:
            archive = _ensure_archive(models_dir, spec["source_url"], spec["archive"])
            print(f"  extracting {spec['member']} ...")
            _extract_member(archive, spec["member"], dest)
        else:
            print(f"  downloading {spec['file']} ...")
            _download(spec["source_url"], dest)
    _register(models_dir, key, spec, allow_record_hash)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models-dir", default="/models", type=Path)
    ap.add_argument("--only", nargs="*", choices=sorted(KNOWN_MODELS), help="subset of keys")
    ap.add_argument(
        "--allow-record-hash",
        action="store_true",
        help="record computed sha256 for models with no pinned hash (first run)",
    )
    args = ap.parse_args()

    models_dir: Path = args.models_dir
    models_dir.mkdir(parents=True, exist_ok=True)
    keys = args.only or list(KNOWN_MODELS)

    failures = 0
    for key in keys:
        try:
            _handle(models_dir, key, KNOWN_MODELS[key], args.allow_record_hash)
        except (mf.ModelIntegrityError, SystemExit, OSError) as exc:
            failures += 1
            print(f"  ERROR: {exc}", file=sys.stderr)

    print(f"\nManifest: {mf.manifest_path(models_dir)}")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
