"""create_session provider selection + graceful CUDA->CPU fallback.

No real onnxruntime/GPU needed: a fake `onnxruntime` module is injected so we
can drive provider availability and force a CUDA init failure deterministically.
"""

from __future__ import annotations

import os
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
    name, cuda_opts = sess.providers[0]
    assert name == "CUDAExecutionProvider"
    # device_id must be threaded through; the memory-growth arena options are
    # asserted loosely so tuning them doesn't re-break this test.
    assert cuda_opts["device_id"] == 2
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


def _fake_nvidia_tree(monkeypatch, tmp_path):
    for lib in ("cudnn", "cublas"):
        (tmp_path / "nvidia" / lib / "bin").mkdir(parents=True)
    (tmp_path / "nvidia" / "cuda_runtime" / "include").mkdir(parents=True)
    monkeypatch.syspath_prepend(str(tmp_path))
    added = []
    monkeypatch.setattr(os, "add_dll_directory", lambda p: added.append(p) or object(), raising=False)
    monkeypatch.setenv("PATH", "orig")
    monkeypatch.setattr(onnx_session, "_dll_dir_handles", [])
    return added


def test_windows_puts_nvidia_bin_dirs_on_the_dll_search_path(monkeypatch, tmp_path):
    # cuDNN 9 loads its engine sub-libraries by name at the first Conv; if the
    # bin dirs aren't searchable the session silently reruns on CPU.
    added = _fake_nvidia_tree(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(onnx_session, "_dlls_preloaded", False)
    mod = _install_fake_ort(monkeypatch, CUDA_AND_CPU)
    order = []
    mod.preload_dlls = lambda: order.append(list(added))
    onnx_session.create_session("m.onnx", device="auto")

    bins = [str(tmp_path / "nvidia" / lib / "bin") for lib in ("cublas", "cudnn")]
    assert added == bins
    assert order == [bins]  # searchable before preload_dlls runs
    assert os.environ["PATH"].split(os.pathsep)[:2] == bins[::-1]


def test_other_platforms_leave_the_dll_search_path_alone(monkeypatch, tmp_path):
    added = _fake_nvidia_tree(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(onnx_session, "_dlls_preloaded", False)
    mod = _install_fake_ort(monkeypatch, CUDA_AND_CPU)
    mod.preload_dlls = lambda: None
    onnx_session.create_session("m.onnx", device="auto")
    assert added == []
    assert os.environ["PATH"] == "orig"
