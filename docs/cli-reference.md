# CLI reference

All commands are subcommands of `synopticon`: 
- Docker: `docker compose run synopticon <command>`
- Local: `uv run synopticon <command>`

## Setup & diagnostics

### `check`
Verify NAS connectivity and credentials (read-only, fast). No options.

### `hwinfo`
Print hardware/environment stats relevant to face detection and grouping — CPU model and core count, RAM, ONNX Runtime version and available execution providers (i.e. whether a GPU is usable), detected NVIDIA GPUs, key library versions, which model weights are present, and disk space. Touches nothing and reaches no network; include its output when filing a bug report. No options.

### `models download`
Download and verify model weights into `storage.models_dir`.

| Option | Default | Description |
|---|---|---|
| `--only TEXT` | all | Subset of model keys to fetch. Repeatable. |
| `--allow-record-hash` | off | Record the sha256 of not-yet-pinned (locally exported) models. |

### `benchmark`
Measure face-detection throughput on your hardware without changing anything. Reuses the `extract` pipeline (detect → align → embed) over a sample of photos but writes no faces, embeddings, or crops. Reports photos/sec, faces/sec, ms/photo, and a per-stage breakdown so you can see where time goes. Originals are downloaded and cached exactly as `extract` would. A short warmup pass runs first so one-off ONNX session/thread-pool startup cost doesn't skew the numbers. Pair with `hwinfo` when tuning `[inference]` threads or comparing CPU vs GPU.

| Option | Default | Description |
|---|---|---|
| `--limit INTEGER` | 25 | Number of photos to time. |
| `--photo-id INTEGER` | none | Benchmark a single photo id. |
| `--space TEXT` | `personal` | Space to pull benchmark photos from. |
| `--warmup INTEGER` | 2 | Photos to process before timing (absorbs ONNX startup cost). |

## Sync

### `sync`
Pull photos, persons, and ground-truth labels from the NAS. Prints live progress per phase (an in-place line on a terminal, periodic lines when piped/in Docker logs). Feeds both the face pipeline and `dedupe` (via `--hash`).

| Option | Default | Description |
|---|---|---|
| `--space TEXT` | all configured | Limit to one space. |
| `--skip-faces` | off | Skip the per-photo face-level ground-truth pass. |
| `--all-faces` | off | Fetch `list_face` for ALL photos, not just those already tagged. |
| `--resume` / `--no-resume` | `--resume` | Resume the faces pass from its saved cursor; `--no-resume` forces a full re-scan. |
| `--hash` | off | Also download each image and store its sha256 + 64-bit perceptual hash (DCT pHash) on the `photos` table. Required before `dedupe`. |

The faces pass is checkpointed per-photo, so it resumes cleanly after an interruption. The items and persons passes are idempotent but restart from the top.

`sync` also records Synology's "similar photo" (stacking) grouping, so review and dedupe deep links point at the group's visible cover photo instead of a non-top-pick member the grouped timeline hides. A failure here (e.g. the feature being unavailable on a space) is logged as a warning and never aborts the rest of `sync`.

`--hash` downloads every image original (videos are skipped), so the first pass is slow on a large library; it commits per photo and skips already-hashed photos on re-runs, re-hashing only photos whose Synology `cache_key` changed (i.e. edited/replaced). Cached originals are evicted afterward unless `storage.keep_originals` is set. Compare `phash` values by hamming distance, not equality — byte-identical duplicates are the `sha256` column's job.

## Face recognition

### `extract` (*Detect faces*)
Scan synced photos, record every face found, and compute ensemble embeddings (resumable; interrupt any time). Evicts cached originals afterward unless `storage.keep_originals` is set.

| Option | Default | Description |
|---|---|---|
| `--limit INTEGER` | none | Process at most N photos. |
| `--photo-id INTEGER` | none | Process a single photo id. |
| `--space TEXT` | all configured | Limit to one space. |

### `cluster` (*Group faces*)
Group all detected faces by person and cross-reference them against Synology persons. Writes a new grouping run. No options.

### `recluster` (*Re-group faces*)
Group the faces again from the cached embeddings with parameter overrides. Never hits the NAS.

| Option | Default | Description |
|---|---|---|
| `--set SECTION.KEY=VALUE` | none | Override a config value, e.g. `--set clustering.edge_threshold=0.47`. Repeatable. |

### `report`
Generate the static HTML review report.

| Option | Default | Description |
|---|---|---|
| `--run-id INTEGER` | latest | Grouping run to report on. |

### `web`
Serve the full web GUI (dashboard, pipeline, review, apply, maintenance, settings) with a first-run setup wizard. Under Docker, add `--service-ports` and `--host 0.0.0.0`, since the default bind is unreachable from outside the container. See [Running with Docker](docker.md#authentication) for authentication and reverse-proxy guidance.

| Option | Default | Description |
|---|---|---|
| `--host TEXT` | `127.0.0.1` | Bind address. Use `0.0.0.0` in a container. |
| `--port INTEGER` | `8686` | Bind port. |

### `review` *(deprecated alias)*
Deprecated alias of `web`, kept for one release. Serves the same app and prints the `/review` URL; new usage should call `synopticon web`. Same `--host`/`--port` options.

### `reset-password`
Reset a web GUI account's password from the shell. This the recovery path when the admin password is lost. It rewrites the scrypt hash in the configured database directly, so it needs access to that database but no login, and it never touches the NAS. All of that account's sessions are revoked (so a stolen cookie can't outlive the credential) unless `--keep-sessions`. With a single account the username can be omitted. This is CLI-only by design and is not exposed as a web job.

```bash
uv run synopticon reset-password              # prompts twice, hidden
docker compose run --rm synopticon reset-password admin
```

| Option | Default | Description |
|---|---|---|
| `[USERNAME]` | the only account | Account to reset; required if several exist. |
| `--password TEXT` | *(prompt)* | New password non-interactively. Prefer the prompt, since this lands in your shell history. |
| `--keep-sessions` | off | Leave that account's existing logins valid. |

### `web-access`
Report or repair who may reach the web interface, from the server. With no options, prints the current `[security]` allowlist settings and which config file they came from. `--clear` is the recovery path an address list locks itself out with - see [Restricting who can reach the GUI](docker.md#restricting-who-can-reach-the-gui). Deliberately CLI-only: a web job would let an already-open session loosen the very list that is about to lock it out on the next restart.

```bash
synopticon web-access                          # report only
synopticon web-access --clear                  # empty the list, then restart
synopticon web-access --allow 192.168.1.0/24   # set the list to one entry
```

| Option | Default | Description |
|---|---|---|
| `--clear` | off | Empty the allowed-address list. |
| `--allow TEXT` | *(none)* | Address or CIDR range to allow (repeatable). |
| `--allow-private / --no-allow-private` | unchanged | Always allow local networks alongside the list above. |

Takes effect on restart, like every `[security]` field this command touches.

### `disable-2fa`
Turn off two-step sign-in for an account, from the server - the recovery path when the device with the authenticator app is lost. Drops the confirmed factor and its backup codes, discards any half-finished sign-in waiting on a code, and revokes every session of that account. Safe to run against an account with no factor enrolled; it reports that and exits cleanly. With a single account the username can be omitted. CLI-only; takes effect immediately, no restart needed.

```bash
uv run synopticon disable-2fa
docker compose run --rm synopticon disable-2fa admin
```

### `session-pin`
Turn off session pinning for an account, from the server — the recovery path out of a pin loop that keeps signing a legitimate browser out. Only `off` can be set from here: pinning to a specific device only makes sense from the browser you want pinned, which the server has no way to identify from a shell prompt. Unpins every existing session in place, without signing anyone out. CLI-only; takes effect immediately, no restart needed.

```bash
uv run synopticon session-pin off
docker compose run --rm synopticon session-pin off admin
```

### `apply`
Apply approved review items to the NAS. Dry-run unless `--apply` is given.

| Option | Default | Description |
|---|---|---|
| `--kinds TEXT` | `assign,low_confidence` | Comma-separated kinds to apply. `assign` and `low_confidence` are the same reviewer-approved face assignment (they differ only in the pipeline's original confidence), so both apply by default; `merge` (an unnamed side is involved) is gated by `--apply-merges`; `merge_named` (joins two already-named people) is gated by the stricter `--apply-merges-named`; `reassign` (opt-in via `--kinds reassign`) is gated by `--apply-reassigns`. |
| `--person-id INTEGER` | none | Scope to a single person id (matches either side of a merge or reassign). |
| `--apply` | off (dry-run) | Actually write to the NAS. |
| `--apply-merges` | off | Extra gate required before an ordinary `merge` (at least one unnamed side) is written. |
| `--apply-merges-named` | off | Extra gate required before a `merge_named` — joining two people who *both* already have a name. Irreversible and destroys a human-assigned label, so it is deliberately not covered by `--apply-merges`. |
| `--apply-reassigns` | off | Extra gate required before any reassign is written — it moves an existing (wrong) Synology face label to a different person via a single reversible `Person.separate` call. |
| `--space TEXT` | `personal` | Space to write to. |
| `--report` | off | Print the audit trail afterward. |

Every run (including dry-runs) appends a per-operation trace to `apply.log` in the project root, one line per writer call — useful for confirming what actually reached the NAS.

### `apply-all`
Apply all approved review items — assigns, low-confidence assigns, reassigns, *and* merges — in one go. Prints a per-kind count of what is about to be written and asks for confirmation; unlike `apply` there is no dry-run stage and the `--apply-merges`/`--apply-reassigns` gates are implicitly lifted. Merges are still irreversible — take a NAS snapshot before a large run.

The one exception is `merge_named` (joining two already-named people): every such pair is listed with a loud warning and gated behind a separate confirmation, so approving the bulk write never silently applies them. When running non-interactively (`-Y`), named→named merges are skipped unless you also pass `--apply-merges-named`.

| Option | Default | Description |
|---|---|---|
| `--yes`, `-Y` | off | Skip the confirmation prompt (for scripted runs). |
| `--apply-merges-named` | off | Include `merge_named` items. Required to apply them with `-Y`; interactively they get their own confirmation prompt instead. |
| `--person-id INTEGER` | none | Scope to a single person id. |
| `--space TEXT` | `personal` | Space to write to. |
| `--report` | off | Print the audit trail afterward. |

### `eval holdout`
Mask a fraction of known labels, group the faces again, and measure recovery.

| Option | Default | Description |
|---|---|---|
| `--mask-fraction FLOAT` | `0.2` | Fraction of known labels to mask. |
| `--seed INTEGER` | `42` | Random seed. |

### `eval corrupt`
Measure how well wrong Synology tags are caught. Moves a sample of correct tags to the wrong person, groups the faces again in memory, and reports how many come back as a reassign to the right person (`recall`), how many reassigns it raised were right (`precision`), and how many untouched tags got a reassign they did not get without the corruption (`new_false_alarms`). `baseline_listed_reassigns` counts the reassigns already proposed without any corruption — some of them are Synology's own mistakes. Never writes to the database or the NAS. Worth running before approving reassigns in bulk, since the `[crossref]` defaults were tuned on a single library.

| Option | Default | Description |
|---|---|---|
| `--fraction FLOAT` | `0.05` | Share of correct tags to move to the wrong person. |
| `--seed INTEGER` | `42` | Random seed. |
| `--mode TEXT` | `both` | Who the wrong person is: `random`, `lookalike` (the most similar other person), or `both`. |

### `eval grid`
Grid-search tunables; writes `eval_grid.csv` under `storage.data_dir`.

| Argument / Option | Default | Description |
|---|---|---|
| `GRID_JSON` (argument) | required | JSON of param→values, e.g. `'{"edge_threshold": [0.45, 0.5, 0.55]}'`. |
| `--mask-fraction FLOAT` | `0.2` | Fraction of known labels to mask. |
| `--seed INTEGER` | `42` | Random seed. |

## Deduplication

### `dedupe`
Delete duplicate photos from the NAS using the content hashes `sync --hash` stores. Two independent levels (pass either or both); within each duplicate group the highest-resolution photo is kept (tie-break: largest file, then lowest id) and the rest are deleted. Hashes are trusted, so there is no review step — but it is dry-run by default and prints a Synology Photos deep link for every photo first, and `--apply` asks for confirmation before writing.

| Option | Default | Description |
|---|---|---|
| `--exact` | off | Delete byte-identical duplicates (grouped by `sha256`). |
| `--visual` | off | Delete visually near-identical duplicates (`phash` within `--threshold` bits; transitively grouped). |
| `--threshold INTEGER` | 5 | Max pHash hamming distance (0–64) for a `--visual` match; lower is stricter. |
| `--apply` | off (dry-run) | Actually delete from the NAS. |
| `--space TEXT` | all configured | Deduplicate within one space (groups never span personal/shared). |
| `--yes`, `-y` | off | Skip the confirmation prompt when deleting. |

Requires a prior `sync --hash` pass to populate the hashes. Each attempt (including dry-runs) is recorded in `audit_log` and `apply.log`. Deletion goes through Synology's `BackgroundTask.File.delete` API as a batched background task; verify on your DSM whether that moves photos to the recycle bin or hard-deletes them before trusting `--apply` on a large run — start with one known duplicate and check.

## Housekeeping

These commands manage locally-computed state and crop images. None of them touch the NAS (except `regen-crops`, which reads originals but never writes).

### `reset`
Clear locally-computed data (faces, embeddings, face groups, and the review queue) plus their crop images, so the pipeline can rebuild from scratch. Useful after tweaking face detection/grouping settings: the review UI pools items from every run, so stale suggestions would otherwise linger. Synced NAS metadata is kept unless `--all` is given. Never touches the NAS.

| Option | Default | Description |
|---|---|---|
| `--all` | off | Also drop synced NAS metadata (photos, persons, ground truth); forces a full re-sync. |
| `--keep-crops` | off | Leave crop images on disk instead of deleting them with the faces. |
| `--yes` / `-y` | off | Skip the confirmation prompt. |

Typical use after changing `[detection]` scores: `synopticon reset` then `synopticon extract && synopticon cluster`.

### `clear-queue`
Delete only the pending review-queue items so the next `cluster` run re-generates them from scratch. Pending rows regenerate cleanly — crossref re-inserts them, picking up any person names synced since the last run (handy when a face was suggested for a person who was still unnamed on the NAS when the faces were grouped). Approved, applied and rejected rows are the ledger crossref uses to avoid re-surfacing work you've already handled, so this command refuses to touch them; use `reset` if you truly want to rebuild everything. Never touches the NAS.

| Option | Default | Description |
|---|---|---|
| `--yes` / `-y` | off | Skip the confirmation prompt. |

Typical use after a `sync` fills in previously-missing person names: `synopticon clear-queue` then `synopticon cluster`.

### `clear-applies`
Send queued-but-unwritten decisions back to `pending` so they turn up in the review UI again. It targets the two statuses that sit between a decision and the NAS: `approved` (waiting for an `apply` that never ran) and `failed` (an apply attempt that errored and is no longer eligible for retry). Both revert to `pending` with the decision metadata cleared — the bulk form of the review UI's per-item undo. Nothing is deleted, so crossref still sees the rows and won't re-surface duplicates; `applied` and `rejected` rows are left alone. Never touches the NAS.

| Option | Default | Description |
|---|---|---|
| `--yes` / `-y` | off | Skip the confirmation prompt. |

Typical use after bulk-approving more than you meant to, or after an `apply` failed partway: `synopticon clear-applies` then re-review.

### `prune-queue`
Delete review items whose faces no longer exist, so they stop cluttering review as cards with no face on them. `review_queue` stores face ids inside its JSON payload with no foreign key, while re-detecting a photo deletes and re-inserts its faces under fresh ids — so every item proposed before a pipeline-version bump points at ids that are gone. Those cards can't be judged and `regen-crops` can't repair them, because the bounding box they'd be rebuilt from went with the old rows.

Only items where *every* referenced face is unrecoverable are removed; a merge whose exemplar list partly survives still renders thumbnails and is left alone. Pending and rejected orphans go by default, since the next `cluster` run re-proposes both from the current faces. Never touches the NAS.

| Option | Default | Description |
|---|---|---|
| `--include-approved` | off | Also drop orphans at the decided statuses (`approved`, `hidden`, `applied`, `failed`). Worth doing for approved rows in particular: `apply` writes those to the NAS from the stored payload alone, so they are corrections nobody could actually see. |
| `--check` | off | Report the counts and exit without deleting anything. |
| `--yes` / `-y` | off | Skip the confirmation prompt. |

Typical use when the review grid fills with faceless cards after a pipeline upgrade: `synopticon prune-queue --check` to see the split by status, then `synopticon prune-queue` and `synopticon cluster`.

### `delete-crops`
Delete every face crop image from disk to reclaim space, leaving the `faces` table (bboxes, landmarks, embeddings) untouched. Crops are a pure derived artifact, so this is safe and reversible — rebuild them on demand with `regen-crops`. Never touches the NAS.

Crop images can accumulate to many gigabytes over a large library. If you run the pipeline intermittently and don't need the review report/UI between passes, wiping crops after each pass keeps disk usage down — the crops cost nothing to recreate from the cached `faces` rows (plus the originals, re-fetched from the NAS) when you next want to review.

| Option | Default | Description |
|---|---|---|
| `--yes` / `-y` | off | Skip the confirmation prompt. |

### `regen-crops`
Rebuild face crop images from the stored bboxes/landmarks and the originals (re-fetched from the NAS), without re-running detection or embedding. Use it to recover from a `delete-crops` (or an accidental wipe), or to repair a partial one. Resumable — it commits per photo. Originals are evicted afterward unless `storage.keep_originals` is set. Reads the NAS but never writes to it.

A crop is two things: the images on disk and the `faces` row pointing at them. Only the row is read downstream — the review UI never looks at the filesystem — so a face whose files exist while its columns are unset shows no crop. Those are repaired with a bare database update, no original fetched (reported as `backfilled`). Faces on a photo that is deleted or missing are out of reach either way; `prune-queue` clears the review items left pointing at them.

| Option | Default | Description |
|---|---|---|
| `--space TEXT` | all | Limit to one space. |
| `--only-missing` / `--all` | `--only-missing` | Only rebuild crops whose files are missing (skips photos already fully on disk, so re-runs are cheap); `--all` rewrites every face's crops. |
| `--limit INTEGER` | none | Process at most N photos. |

### `db-migrate`
Copy an existing library into the configured database backend — the one-time move you need after switching `[database] backend` to `postgres`, so the switch doesn't cost a fresh `sync` and a multi-hour re-`extract`. See [Database backend](configuration.md#database-backend). Never touches the NAS.

The destination is whatever `[database]` currently selects; the source defaults to the SQLite file that backend replaced. It refuses to run if the destination already holds data, and it copies primary keys verbatim, so deep links, review decisions and the audit trail all survive.

| Option | Default | Description |
|---|---|---|
| `--from TEXT` | the SQLite file under `data_dir` | Database to copy out of (a path, or a `postgresql://` URL). |
| `--yes` / `-y` | off | Skip the confirmation prompt. |

Run it once, with nothing else touching either database.
