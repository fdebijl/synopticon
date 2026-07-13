# Synopticon — multi-stage build.
# `cpu` (default) installs onnxruntime; `gpu` installs onnxruntime-gpu on a CUDA
# base. Build the GPU image with: --target gpu --build-arg ORT_EXTRA=gpu
# (running it needs the NVIDIA Container Toolkit; see docker-compose.yml).

FROM python:3.11-slim-bookworm AS builder
# Which ONNX Runtime extra to install: "cpu" (onnxruntime) or "gpu" (onnxruntime-gpu).
ARG ORT_EXTRA=cpu
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev --extra "${ORT_EXTRA}" --extra review --extra faiss
COPY . .
RUN uv sync --frozen --no-dev --extra "${ORT_EXTRA}" --extra review --extra faiss

FROM python:3.11-slim-bookworm AS cpu
# libgl/libglib needed by opencv-python-headless's transitive loaders on some distros
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/scripts /app/scripts
ENV PATH="/opt/venv/bin:$PATH" \
    SYNOPTICON_STORAGE__DATA_DIR=/data \
    SYNOPTICON_STORAGE__MODELS_DIR=/models
VOLUME ["/data", "/models"]
WORKDIR /app
ENTRYPOINT ["synopticon"]
CMD ["--help"]

# GPU image. Build with --build-arg ORT_EXTRA=gpu so the copied venv contains
# onnxruntime-gpu[cuda,cudnn] -- the NVIDIA CUDA + cuDNN runtime libraries come
# from those pip wheels (preload_dlls locates them), so no CUDA base image is
# needed. Only the driver is required, injected at run time by the NVIDIA
# Container Toolkit (see docker-compose.yml's gpu profile).
FROM python:3.11-slim-bookworm AS gpu
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/scripts /app/scripts
ENV PATH="/opt/venv/bin:$PATH" \
    SYNOPTICON_STORAGE__DATA_DIR=/data \
    SYNOPTICON_STORAGE__MODELS_DIR=/models \
    SYNOPTICON_INFERENCE__DEVICE=cuda
VOLUME ["/data", "/models"]
WORKDIR /app
ENTRYPOINT ["synopticon"]
CMD ["--help"]
