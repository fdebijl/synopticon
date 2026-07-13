"""create_session provider selection + graceful CUDA->CPU fallback.

No real onnxruntime/GPU needed: a fake `onnxruntime` module is injected so we
can drive provider availability and force a CUDA init failure deterministically.
"""

from __future__ import annotations

import sys
import types

from synopticon.pipeline import onnx_session

CUDA_AND_CPU = ["CUDAExecutionProvider", "CPUExecutionProvider"]
CPU_ONLY = ["CPUExecutionProvider"]


class _FakeSessionOptions:
    def __init__(self):
        self.intra_op_num_threads = None


class _FakeSession:
    def __init__(self, path, opts, providers):
        self.path = path
        self.opts = opts
        self.providers = providers


def _install_fake_ort(monkeypatch, available, *, fail_on_cuda=False):
    mod = types.ModuleType("onnxruntime")
    mod.SessionOptions = _FakeSessionOptions
    mod.get_available_providers = lambda: list(available)

    def _make_session(path, opts, providers=None):
        has_cuda = any(
            (p[0] if isinstance(p, tuple) else p) == "CUDAExecutionProvider"
            for p in providers
        )
        if fail_on_cuda and has_cuda:
            raise RuntimeError("CUDA init boom")
        return _FakeSession(path, opts, providers)

    mod.InferenceSession = _make_session
    monkeypatch.setitem(sys.modules, "onnxruntime", mod)
    return mod


def test_cuda_used_with_device_id_when_available(monkeypatch):
    _install_fake_ort(monkeypatch, CUDA_AND_CPU)
    sess = onnx_session.create_session("m.onnx", device="auto", device_id=2)
    assert sess.providers[0] == ("CUDAExecutionProvider", {"device_id": 2})
    assert "CPUExecutionProvider" in sess.providers


def test_cuda_init_failure_falls_back_to_cpu(monkeypatch):
    _install_fake_ort(monkeypatch, CUDA_AND_CPU, fail_on_cuda=True)
    sess = onnx_session.create_session("m.onnx", device="cuda")
    assert sess.providers == ["CPUExecutionProvider"]


def test_cpu_device_never_requests_cuda(monkeypatch):
    _install_fake_ort(monkeypatch, CUDA_AND_CPU)
    sess = onnx_session.create_session("m.onnx", device="cpu")
    assert sess.providers == ["CPUExecutionProvider"]


def test_cuda_requested_but_provider_absent_falls_back(monkeypatch):
    _install_fake_ort(monkeypatch, CPU_ONLY)
    sess = onnx_session.create_session("m.onnx", device="cuda")
    assert sess.providers == ["CPUExecutionProvider"]


def test_intra_op_threads_honored(monkeypatch):
    _install_fake_ort(monkeypatch, CPU_ONLY)
    sess = onnx_session.create_session("m.onnx", device="cpu", intra_op_threads=3)
    assert sess.opts.intra_op_num_threads == 3


def test_preload_dlls_called_before_cuda_session(monkeypatch):
    # preload_dlls() must run before the CUDA session, else ORT can't find the
    # pip-wheel CUDA libs and silently drops to CPU.
    monkeypatch.setattr(onnx_session, "_dlls_preloaded", False)
    mod = _install_fake_ort(monkeypatch, CUDA_AND_CPU)
    calls = []
    mod.preload_dlls = lambda: calls.append(True)
    onnx_session.create_session("m.onnx", device="auto")
    assert calls == [True]


def test_no_preload_on_cpu_device(monkeypatch):
    monkeypatch.setattr(onnx_session, "_dlls_preloaded", False)
    mod = _install_fake_ort(monkeypatch, CUDA_AND_CPU)
    calls = []
    mod.preload_dlls = lambda: calls.append(True)
    onnx_session.create_session("m.onnx", device="cpu")
    assert calls == []
