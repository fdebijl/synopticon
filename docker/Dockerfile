# Synopticon — multi-stage build.
# Default target "cpu" is the supported image; "gpu" is provided but untested.

FROM python:3.11-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev --extra review --extra faiss
COPY . .
RUN uv sync --frozen --no-dev --extra review --extra faiss

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

# Untested convenience target: swaps onnxruntime for onnxruntime-gpu on a CUDA base.
FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04 AS gpu
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/scripts /app/scripts
RUN /opt/venv/bin/pip uninstall -y onnxruntime && /opt/venv/bin/pip install onnxruntime-gpu
ENV PATH="/opt/venv/bin:$PATH" \
    SYNOPTICON_STORAGE__DATA_DIR=/data \
    SYNOPTICON_STORAGE__MODELS_DIR=/models \
    SYNOPTICON_INFERENCE__DEVICE=cuda
VOLUME ["/data", "/models"]
WORKDIR /app
ENTRYPOINT ["synopticon"]
CMD ["--help"]
