#!/usr/bin/env python3
"""Export a MagFace iResNet100 checkpoint to ONNX (opset 17, dynamic batch).

Run once, manually (needs the ``export`` extra: torch + onnx)::

    uv run --extra export python scripts/export_magface_onnx.py \\
        --checkpoint magface_iresnet100.pth --out /models/magface_iresnet100.onnx

Preprocessing the exported model expects (matches embed/magface.py):
    BGR channel order, (x/255 - 0.5)/0.5 == (x - 127.5)/127.5, input 1x3x112x112.

The iResNet backbone is vendored below (compact, faithful to
IrvingMeng/MagFace ``models/iresnet.py``, which follows the insightface
iresnet). All torch/onnx imports are inside main() so this file imports without
torch installed.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def _build_net_module():
    """Return (nn, iresnet100_factory). Imports torch lazily."""
    import torch
    import torch.nn as nn

    def conv3x3(in_planes, out_planes, stride=1, groups=1, dilation=1):
        return nn.Conv2d(
            in_planes, out_planes, kernel_size=3, stride=stride,
            padding=dilation, groups=groups, bias=False, dilation=dilation,
        )

    def conv1x1(in_planes, out_planes, stride=1):
        return nn.Conv2d(in_planes, out_planes, kernel_size=1, stride=stride, bias=False)

    class IBasicBlock(nn.Module):
        expansion = 1

        def __init__(self, inplanes, planes, stride=1, downsample=None):
            super().__init__()
            self.bn1 = nn.BatchNorm2d(inplanes, eps=1e-05)
            self.conv1 = conv3x3(inplanes, planes)
            self.bn2 = nn.BatchNorm2d(planes, eps=1e-05)
            self.prelu = nn.PReLU(planes)
            self.conv2 = conv3x3(planes, planes, stride)
            self.bn3 = nn.BatchNorm2d(planes, eps=1e-05)
            self.downsample = downsample
            self.stride = stride

        def forward(self, x):
            identity = x
            out = self.bn1(x)
            out = self.conv1(out)
            out = self.bn2(out)
            out = self.prelu(out)
            out = self.conv2(out)
            out = self.bn3(out)
            if self.downsample is not None:
                identity = self.downsample(x)
            out += identity
            return out

    class IResNet(nn.Module):
        fc_scale = 7 * 7

        def __init__(self, layers, num_features=512, dropout=0.0):
            super().__init__()
            self.inplanes = 64
            self.conv1 = nn.Conv2d(3, self.inplanes, kernel_size=3, stride=1, padding=1, bias=False)
            self.bn1 = nn.BatchNorm2d(self.inplanes, eps=1e-05)
            self.prelu = nn.PReLU(self.inplanes)
            self.layer1 = self._make_layer(64, layers[0], stride=2)
            self.layer2 = self._make_layer(128, layers[1], stride=2)
            self.layer3 = self._make_layer(256, layers[2], stride=2)
            self.layer4 = self._make_layer(512, layers[3], stride=2)
            self.bn2 = nn.BatchNorm2d(512, eps=1e-05)
            self.dropout = nn.Dropout(p=dropout)
            self.fc = nn.Linear(512 * self.fc_scale, num_features)
            self.features = nn.BatchNorm1d(num_features, eps=1e-05)

        def _make_layer(self, planes, blocks, stride=1):
            downsample = None
            if stride != 1 or self.inplanes != planes:
                downsample = nn.Sequential(
                    conv1x1(self.inplanes, planes, stride),
                    nn.BatchNorm2d(planes, eps=1e-05),
                )
            layers = [IBasicBlock(self.inplanes, planes, stride, downsample)]
            self.inplanes = planes
            for _ in range(1, blocks):
                layers.append(IBasicBlock(self.inplanes, planes))
            return nn.Sequential(*layers)

        def forward(self, x):
            x = self.conv1(x)
            x = self.bn1(x)
            x = self.prelu(x)
            x = self.layer1(x)
            x = self.layer2(x)
            x = self.layer3(x)
            x = self.layer4(x)
            x = self.bn2(x)
            x = torch.flatten(x, 1)
            x = self.dropout(x)
            x = self.fc(x)
            x = self.features(x)
            return x

    def iresnet100(**kwargs):
        return IResNet([3, 13, 30, 3], **kwargs)

    return torch, nn, iresnet100


def _load_state_dict(torch, net, checkpoint: Path):
    ckpt = torch.load(str(checkpoint), map_location="cpu")
    state = ckpt.get("state_dict", ckpt.get("model", ckpt)) if isinstance(ckpt, dict) else ckpt
    cleaned = {}
    for k, v in state.items():
        nk = k
        for prefix in ("module.", "features.module.", "backbone.", "model."):
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

    torch, _nn, iresnet100 = _build_net_module()

    net = iresnet100()
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
