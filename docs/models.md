# Models

The face-recognition pipeline needs model weights; deduplication and the housekeeping commands do not. Weights are not bundled (mixed licenses; some upstream terms are research-only — verify they fit your use). `synopticon models download` fetches what it can and prints instructions for the rest:

| Model | Role | Source | Availability |
|---|---|---|---|
| SCRFD-10G-KPS | primary detector | insightface `antelopev2` | auto-download |
| ArcFace R100 (Glint360K) | embedder | insightface `antelopev2` | auto-download |
| YOLOv8-l-face | secondary detector | lindevs/yolov8-face | auto-download |
| AdaFace IR-101 (WebFace12M) | embedder | official checkpoint → `scripts/export_adaface_onnx.py` | optional, manual export |
| MagFace iResNet100 | embedder + quality signal | official checkpoint → `scripts/export_magface_onnx.py` | optional, manual export |

The pipeline degrades gracefully to whatever subset is present (minimum: SCRFD + one embedder). Every model is sha256-verified against `models/manifest.json` on load; the auto-downloaded models are pinned upstream, and locally-exported ones are registered with `models download --allow-record-hash`. Face restoration (CodeFormer) is off by default and lives behind the `restore` extra due to its pinned-torch dependency chain.

## Exporting AdaFace and MagFace

AdaFace and MagFace can't be auto-downloaded — their weights aren't redistributed, so you fetch the official checkpoint yourself and convert it to ONNX with the bundled export scripts. Both scripts take `--checkpoint` and `--out`.

The export scripts depend on `torch==2.1.2` (pinned to match the `restore` extra's constraints), which predates NumPy 2 and cannot interop with the project's `numpy>=2.4.6` pin. So **don't run them via `--extra export` inside the project venv** — the ONNX still traces, but the built-in torch-vs-ONNX numerical check crashes with `Numpy is not available`, leaving the file unverified. Instead run each script in an isolated, project-free env on Python 3.11 with `numpy<2`, so the verification actually runs:

**AdaFace IR-101 (WebFace12M):** grab `adaface_ir101_webface12m.ckpt` from the "Pretrained Models" table in the [mk-minchul/AdaFace](https://github.com/mk-minchul/AdaFace) README (the `IR-101` / `WebFace12M` row), then:

```bash
uv run --no-project --python 3.11 \
  --with "torch==2.1.2" --with "onnx>=1.15" --with "numpy<2" \
  python scripts/export_adaface_onnx.py \
  --checkpoint adaface_ir101_webface12m.ckpt \
  --out models/adaface_ir101_webface12m.onnx
```

**MagFace iResNet100:** grab `magface_iresnet100.pth` from the "Model Zoo" section of the [IrvingMeng/MagFace](https://github.com/IrvingMeng/MagFace) README (the iResNet100 backbone), then:

```bash
uv run --no-project --python 3.11 \
  --with "torch==2.1.2" --with "onnx>=1.15" --with "numpy<2" \
  python scripts/export_magface_onnx.py \
  --checkpoint magface_iresnet100.pth \
  --out models/magface_iresnet100.onnx
```

A successful run prints `torch-vs-ort cosine: 1.000000` and `OK`. The AdaFace export additionally warns about a few unexpected `head.*` keys — that's the training-time margin head, correctly ignored; only the backbone embedding is exported. Once the `.onnx` files exist in `models/`, re-run `synopticon models download --allow-record-hash` to verify and register them in the manifest.

In the web GUI, the same step is the Download models card on the Pipeline page — tick 'record hash' to register manually-copied `.onnx` files — and Settings → Models shows which weights are present and registered.
