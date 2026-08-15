# ADR 05 — NAS write safety model

**Status:** Accepted — do not weaken
**Applies to:** `syno/writeback.py`, `cli.py`'s `apply`/`apply-all`, `web/jobs.py`, `web/schedules.py`, `web/quickmerger.py`, `cluster/crossref.py`

## Context

Synopticon writes corrections into a photo library that a human curated by hand. Some of those
writes are reversible; one class is not. A merge that joins two already-named people destroys a
label a person typed, and no API call brings it back.

The tool is also driven from three surfaces — CLI, web GUI, and cron-style schedules — and a
safety model that only holds on one of them is not a safety model.

## Decision

Danger is classified at generation time, gated by a flag per tier, and every surface is
constrained so that it cannot reach a tier the CLI would have required an explicit flag for.

### Everything before `apply` is read-only

Sync, extract, cluster, crossref and review never write to the NAS. `apply` is the boundary.

### `apply` is dry-run by default, with a flag per danger tier

| Tier | Gate | Reversible? |
|---|---|---|
| assign / low_confidence / new_person | `--apply` | yes |
| reassign | `--apply --apply-reassigns` | yes — moves a face-label a human can already see in Photos |
| merge (at least one side unnamed) | `--apply --apply-merges` | **no** |
| merge_named (both sides named) | `--apply --apply-merges-named` | **no**, and destroys a human label |

`--apply-merges` never covers `merge_named`. That is the point of splitting them.

Classification happens at generation time in `crossref.run_clustering` — both `persons.name`
non-empty produces a `merge_named` review kind. Migration `0005` reclassified any pre-existing
un-applied both-named `merge` rows.

### `apply-all` lifts the ordinary gates but not the named one

`apply-all` writes every approved kind at once with the merge/reassign gates implicitly lifted.
`merge_named` is the exception: it lists every named↔named pair with a loud warning and requires a
*separate* confirmation (or `--apply-merges-named` under `-Y`), so the bulk confirm never sweeps
them in. It still confirms interactively and never dry-runs.

### Invariants that hold for every write

- Only `review_queue` rows with `status='approved'` are eligible.
- Every write attempt lands in `audit_log`.
- An idempotency pre-check re-fetches NAS state immediately before each write.

## Surface constraints

### The web GUI: an allowlist, never raw argv

`JOB_SPECS: dict[str, JobSpec]` maps a job name to a `build_argv(params)` parameter whitelist plus
a `DangerLevel` (SAFE / CONFIRM / TYPED_PHRASE). `validate_consent` is the *sole* place
`--apply*`/`-y` flags are appended, gated by the request's `confirm`, gate-boolean, or
`confirm_phrase`. A missing gate raises `ConsentError` → HTTP 428, and the response never leaks
the phrase.

**Hard rule: the GUI must never pass `-Y` or use `apply-all`** — enforced by
`_FORBIDDEN_TOKENS = {"apply-all", "-Y"}`, which `resolve_argv` and `submit` refuse.

| Job / form | Consent required | Flags appended |
|---|---|---|
| apply dry-run | none (free preview) | *(no `--apply`)* |
| apply assign/low_confidence/new_person | `confirm` | `--apply` |
| apply reassign | `confirm` + `apply_reassigns` | `--apply --apply-reassigns` |
| apply merge | `confirm` + `apply_merges` | `--apply --apply-merges` |
| apply merge_named | `confirm_phrase == "merge named people"` | `--apply --apply-merges-named` |
| dedupe `--apply` | `confirm_phrase == "delete duplicates"` | `--apply -y` |
| reset `--all` | `confirm_phrase == "reset all"` | `-y` |
| reset / clear-queue / delete-crops | `confirm` | `-y` |

`recluster` whitelists only `clustering.*` and `crossref.*` override keys — no arbitrary `--set`.
`models-download` is SAFE and whitelists `only` plus an `allow_record_hash` boolean
(→ `--allow-record-hash`, for registering ONNX files copied into the models directory by hand,
such as the AdaFace/MagFace exports).

Three commands are **CLI-only and must never gain a `JOB_SPECS` entry**:

- `eval` — no reason to expose it.
- `reset-password` — a web job would let an already-authenticated session rewrite the credential.
- `db-migrate` — it rewrites the destination database wholesale (ADR 09).

### Schedules replay a submission, never an argv

`web/schedules.py` + `web/scheduler.py` store a *submission* and replay it through the same
`JobManager.submit` path, so the allowlist, parameter whitelist and `validate_consent` all re-run
at fire time.

Their structural guarantee is that **`confirm_phrase` is always `None`**, both at save time and at
fire time. Every typed-phrase form — `merge_named`, `dedupe --apply`, `reset --all` — therefore
raises `ConsentError` and is unschedulable by construction rather than by UI convention.

`SCHEDULABLE` is additionally a strict subset of `JOB_SPECS`. `reset` is excluded: it is only
confirm-tier, but a periodic wipe of local state has no legitimate use.

### QuickMerger is the one exception, with equivalent guarantees

`web/quickmerger.py` is the single exception to "the GUI writes only through `apply`". It is an
interactive triage tool, so per-item dialog gating would destroy the flow it exists to provide.

Its equivalent guarantees:

- Every write needs `confirm: true` in the request body, else 428 — the job layer's consent code.
- `SynoWriter(action_prefix="quickmerger")` audits every attempt.
- A merge re-reads **both** people from the NAS immediately before writing and **refuses with 409
  if the merged-away side has a name**. Named↔named merges are structurally unreachable from that
  surface, the same way `apply-all` and `-Y` are unreachable from `JOB_SPECS`.

Naming and hiding are reversible, which is why only the merge motivates the frontend's
once-per-session confirmation.

## Consequences

- Adding a new write operation means deciding its tier first, then its `JOB_SPECS` danger level,
  then whether it is schedulable. In that order.
- Any new surface that can reach `SynoWriter` needs its own answer to "how can this not perform a
  named merge?"
