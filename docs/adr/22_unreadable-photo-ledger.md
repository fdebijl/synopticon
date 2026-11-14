# ADR 22 — Remembering photos detection cannot read

**Status:** Accepted
**Applies to:** migration `0013_extract_skips.sql`, `pipeline/runner.py` (`UNREADABLE_REASONS`, `_fetch_work`, `_record_unreadable`), `extract --retry-skipped` and its `JOB_SPECS` param, `cli.py::_DERIVED_TABLES`, `db/copy.py::TABLES`

## Context

`extract_log` is written in the same transaction as a photo's faces, so it records only photos that
succeeded. A photo that failed was selected again on the next run, downloaded again in full, and
failed again: forever. A user library (issue #87) reported 468 camera RAW files and ~1,500 Live
Photo motion clips that Pillow cannot decode, about 16 GB re-downloaded and rejected on every run. They also
have the lowest ids, so `ORDER BY p.id` put them first in every run, which made throughput look like
it fell off a cliff once that block ended.

## Decision

A separate `extract_skips` table records a photo the pipeline could not read, keyed like
`extract_log` on `(space, photo_id)` with the `cache_key` and `pipeline_version` it failed under.
`_fetch_work` leaves out a photo whose row still matches both. A success deletes the row in the
photo's own transaction.

**Only failures the file itself causes are recorded.** `UNREADABLE_REASONS` holds the skip reasons a
retry of the same bytes under the same pipeline cannot change; today that is just "not a decodable
image". Everything else is retried as before: NAS errors, a full disk, `MemoryError`, a HEIC file
while pillow-heif is missing, and any unexpected exception, since a code fix changes the outcome
without bumping `pipeline_version` (the SCRFD anchor-cache `KeyError` in the same issue was one of
those).

A record lapses when the file changes (`cache_key`) or the pipeline does (`pipeline_version`), and
`extract --retry-skipped` ignores the table. That is the way back for a fix the version cannot see,
such as adding a decoder. `--photo-id` already bypasses every selection filter and still does.

## Alternatives considered

- **A status column on `extract_log`.** Every reader of `extract_log` (coverage, the Stats page,
  Inspect, benchmark) treats a row as "faces were detected here" and would have needed a filter. A
  separate table leaves them correct by construction.
- **Filtering by file suffix** (`.cr2`, `.cr3`, `.mov`, …). It needs no download at all, but it is a
  guess about what Pillow can decode, wrong the day a decoder is added, and blind to an undecodable
  file with an ordinary name. The ledger costs one download per file and is never wrong about what
  failed.

## Consequences

- An undecodable photo still costs one download, once per `cache_key` and `pipeline_version`.
- `ExtractStats.unreadable` counts the photos left out this way, and the run logs it with a pointer to
  `--retry-skipped`, so they are not invisible.
- `reset` clears the table with the other derived tables, and `db-migrate` copies it.
