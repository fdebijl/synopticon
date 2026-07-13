#!/usr/bin/env python3
"""Export an AdaFace IR-101 (WebFace12M) checkpoint to ONNX (opset 17, dynamic batch).

Run once, manually (needs the ``export`` extra: torch + onnx)::

    uv run --extra export python scripts/export_adaface_onnx.py \\
        --checkpoint adaface_ir101_webface12m.ckpt \\
        --out /models/adaface_ir101_webface12m.onnx

Preprocessing the exported model expects (matches embed/adaface.py):
    BGR channel order, (x/255 - 0.5)/0.5 == (x - 127.5)/127.5, input 1x3x112x112.

The IR-101 backbone is vendored below (compact, faithful to mk-minchul/AdaFace
``net.py`` — IR blocks, get_blocks(num_layers=100)). All torch/onnx imports are
inside main() so this file imports without torch installed.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def _build_net_module():
    """Return (torch, ir_101 factory). Imports torch lazily."""
    import torch
    import torch.nn as nn

    class Flatten(nn.Module):
        def forward(self, x):
            return x.view(x.size(0), -1)

    class BottleneckIR(nn.Module):
        def __init__(self, in_channel, depth, stride):
            super().__init__()
            if in_channel == depth:
                self.shortcut_layer = nn.MaxPool2d(1, stride)
            else:
                self.shortcut_layer = nn.Sequential(
                    nn.Conv2d(in_channel, depth, (1, 1), stride, bias=False),
                    nn.BatchNorm2d(depth),
                )
            self.res_layer = nn.Sequential(
                nn.BatchNorm2d(in_channel),
                nn.Conv2d(in_channel, depth, (3, 3), (1, 1), 1, bias=False),
                nn.PReLU(depth),
                nn.Conv2d(depth, depth, (3, 3), stride, 1, bias=False),
                nn.BatchNorm2d(depth),
            )

        def forward(self, x):
            return self.res_layer(x) + self.shortcut_layer(x)

    def get_block(in_channel, depth, num_units):
        units = [(in_channel, depth, 2)]
        units += [(depth, depth, 1) for _ in range(num_units - 1)]
        return units

    class Backbone(nn.Module):
        def __init__(self, input_size=112, num_features=512):
            super().__init__()
            blocks = [
                get_block(64, 64, 3),
                get_block(64, 128, 13),
                get_block(128, 256, 30),
                get_block(256, 512, 3),
            ]
            self.input_layer = nn.Sequential(
                nn.Conv2d(3, 64, (3, 3), 1, 1, bias=False),
                nn.BatchNorm2d(64),
                nn.PReLU(64),
            )
            out_hw = input_size // 16  # four stride-2 stages -> 112 -> 7
            self.output_layer = nn.Sequential(
                nn.BatchNorm2d(512),
                nn.Dropout(0.4),
                Flatten(),
                nn.Linear(512 * out_hw * out_hw, num_features),
                nn.BatchNorm1d(num_features),
            )
            modules = []
            for block in blocks:
                for in_channel, depth, stride in block:
                    modules.append(BottleneckIR(in_channel, depth, stride))
            self.body = nn.Sequential(*modules)

        def forward(self, x):
            x = self.input_layer(x)
            x = self.body(x)
            x = self.output_layer(x)
            # AdaFace returns (embedding, norm); we export the raw embedding and
            # L2-normalize at inference time (embed/adaface.py).
            return x

    def ir_101(**kwargs):
        return Backbone(input_size=112, **kwargs)

    return torch, ir_101


def _load_state_dict(torch, net, checkpoint: Path):
    ckpt = torch.load(str(checkpoint), map_location="cpu")
    if isinstance(ckpt, dict):
        state = ckpt.get("state_dict", ckpt.get("model", ckpt))
    else:
        state = ckpt
    cleaned = {}
    for k, v in state.items():
        nk = k
        for prefix in ("model.", "module.", "backbone.", "features."):
            if nk.startswith(prefix):
                nk = nk[len(prefix):]
        cleaned[nk] = v
    missing, unexpected = net.load_state_dict(cleaned, strict=False)
    if missing:
        print(f"  WARNING: {len(missing)} missing keys (first few): {missing[:5]}")
    if unexpected:
        print(f"  WARNING: {len(unexpected)} unexpected keys (first few): {unexpected[:5]}")
    return net


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--checkpoint", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--opset", type=int, default=17)
    args = ap.parse_args()

    import numpy as np
    import onnxruntime as ort

    torch, ir_101 = _build_net_module()

    net = ir_101()
    net.eval()
    _load_state_dict(torch, net, args.checkpoint)

    dummy = torch.randn(1, 3, 112, 112)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    torch.onnx.export(
        net, dummy, str(args.out),
        input_names=["input"], output_names=["embedding"],
        dynamic_axes={"input": {0: "batch"}, "embedding": {0: "batch"}},
        opset_version=args.opset, do_constant_folding=True,
    )
    print(f"  exported {args.out}")

    with torch.no_grad():
        ref = net(dummy).cpu().numpy().ravel()
    sess = ort.InferenceSession(str(args.out), providers=["CPUExecutionProvider"])
    got = sess.run(None, {"input": dummy.numpy()})[0].ravel()
    cos = float(np.dot(ref, got) / (np.linalg.norm(ref) * np.linalg.norm(got) + 1e-12))
    print(f"  torch-vs-ort cosine: {cos:.6f}")
    if cos < 0.9999:
        args.out.unlink(missing_ok=True)
        raise SystemExit(f"cosine {cos:.6f} < 0.9999 — export mismatch, removed {args.out}")
    print("  OK")


if __name__ == "__main__":
    main()
