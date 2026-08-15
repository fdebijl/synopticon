# ADR 10 — Two Docker runtime variants from one Dockerfile

**Status:** Accepted
**Applies to:** `docker/Dockerfile`, `.github/workflows/docker-publish.yml`, README's "Docker images" section

## Context

Synopticon runs CPU-first, with optional CUDA. ONNX Runtime ships those as two mutually exclusive
packages (`onnxruntime` and `onnxruntime-gpu`) that share one import namespace, so the `cpu` and
`gpu` extras are declared conflicting in `pyproject.toml`. That means `--all-extras` does not
resolve, and it means an image must commit to one.

Publishing them as two separate Docker Hub repositories would double the documentation surface and
split the tag history.

## Decision

One multi-stage `docker/Dockerfile` with two runtime targets, `cpu` and `gpu`, published to one
Docker Hub repository (`fdebijl/synopticon`) with the variant in the **tag suffix**.

### The build argument must match the target

The target is selected together with a matching `--build-arg ORT_EXTRA=cpu|gpu`. That argument
drives which conflicting ONNX Runtime extra the builder stage installs. **Pass the wrong one and
you get a runtime whose target and wheels disagree** — a GPU base image with CPU wheels, or the
reverse, which fails at model load rather than at build.

### Tag scheme

| Tags | Variant |
|---|---|
| `latest`, `cpu`, `0.1.0`, `0.1.0-cpu` | CPU |
| `gpu`, `0.1.0-gpu` | CUDA |
| `sha-<short>-<variant>` | both, per commit |

`latest` and the unsuffixed semver aliases move only on a `v*` tag push.

Pushes to main build both variants without publishing, as a Dockerfile smoke test. `linux/amd64`
only — NVIDIA ships no arm64 CUDA wheels.

Publishing needs the `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` repository secrets.

Keep the tag table in README's "Docker images" section in sync with the workflow's
`metadata-action` tag list.

## Deployment consequences

### Bind address

**`synopticon web` binds `127.0.0.1` by default, which is unreachable from outside a container.**
Every Docker invocation in the docs must pass `--host 0.0.0.0` and rely on host-side port
publishing (`127.0.0.1:8686:8686`) for the actual access control. Getting this backwards — binding
`0.0.0.0` and publishing on all interfaces — exposes an unauthenticated-until-setup GUI to the
network.

### No `HEALTHCHECK` in the image

The same entrypoint serves one-shot commands, which a built-in probe would mark unhealthy.
README's "Healthchecks" section carries the service-level recipe instead, pointed at
`GET /api/health` (ADR 07).

### Storage paths

Storage defaults are the repo-root `./data` and `./models`. The image overrides both via
environment variables to its volume mounts — the same directories on disk either way.
