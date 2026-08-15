# ADR 01 — Offline clustering boundary and leaf modules

**Status:** Accepted
**Applies to:** `cluster/`, `pipeline/runner.py`, `cli.py`, `cpu.py`, `progress.py`, `cron.py`, `links.py`, `pipeline/version.py`, `dedupe.py`

## Context

Synopticon is a pipeline of five CLI phases whose only contract with each other is the
database. Two forces push against casual imports:

1. **Reclustering must work with the NAS unreachable.** Tuning clustering parameters is an
   iterative, offline activity over embeddings already cached in the database. If `cluster/`
   could reach the Synology layer, a network dependency would eventually creep in and
   `recluster` would stop working on a plane.
2. **The web process must not pay the pipeline's import cost.** `cv2` and `onnxruntime` take
   20+ seconds to page in. Importing them inside a request handler is a hard stall — see
   ADR 07.

Both are import-graph problems, so both get solved the same way.

## Decision

A hard, unidirectional module boundary, plus a set of deliberately dependency-free leaf
modules that anything may import.

**The boundary:**

- `cluster/` must never import from `syno/` or `pipeline/`.
- `pipeline/runner.py` takes a `fetch_original(row) -> Path` callable rather than importing
  `sync/`, so the pipeline does not depend on the download layer either.
- `dedupe.py` (detection) is NAS-free — pure database reads over `photos`. `dedupe_writeback.py`
  is the only half that touches the NAS.
- `cli.py` is where the layers get wired together. It is the only module allowed to know about
  all of them.

**The leaf modules** are dependency-free by construction, so importing one can never pull a
heavier layer in behind it:

| Module | Contents | Why it is a leaf |
|---|---|---|
| `cpu.py` | `available_cores()`, `physical_cores()` | `cluster/` and the web process both need core counts |
| `progress.py` | the progress emitter | `cluster/` emits progress without breaching the boundary |
| `cron.py` | 5-field Vixie parser + `next_fire` | scheduling needs no application code |
| `links.py` | `syno_web_base()`, `item_url`, `person_url` | every layer builds deep links |
| `pipeline/version.py` | `pipeline_version` (hashlib + `manifest_bytes`) | the web process resolves it without `cv2` |

## Consequences

- Deep links are built in exactly one place. `sync/`, `pipeline/`, `review/`, `web/` and `cli.py`
  share one URL shape, and none of them needs to import a heavier layer to make a link.
  `review/queries.py` re-exports the three names for its existing callers (`web/quickmerger.py`).
- `pipeline/runner.py` re-exports `pipeline_version` for the CLI's sake, but the definition
  stays in the leaf module.
- `pipeline/crops.py` defers its `align`/`runner` imports into `regen_crops`/`_crops_present`,
  so `ops_routes` can call `crops_disk_usage` — a plain directory walk — without the image stack.
- Adding a dependency to any leaf module is a breaking change to several unrelated callers.
  Check the table above before doing it.
