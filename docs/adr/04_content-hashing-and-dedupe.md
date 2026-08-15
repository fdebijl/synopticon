# ADR 04 — Content hashing and duplicate deletion

**Status:** Accepted
**Applies to:** `sync/hashes.py`, `dedupe.py`, `dedupe_writeback.py`

## Context

A synced photo library accumulates two kinds of duplicate: byte-identical re-imports, and visually
identical shots that differ in bytes (re-encoded, rotated, resized). Detecting them needs two
different hashes, and deleting them touches the one operation in the whole system that destroys
user data on the NAS.

## Decision

### Two hashes, computed by an opt-in pass

`sync --hash` populates `photos.sha256`, `phash`, `hash_cache_key` and `hashed_at` by downloading
each original. Videos are excluded; `live` photos are included. Like the pipeline, it takes a
`fetch_original(row) -> Path` callable and commits per photo, so it is resumable by construction.

- **`sha256`** detects byte-identical duplicates.
- **`phash`** is a 64-bit DCT perceptual hash rendered as 16 hex characters. **Compare it by
  hamming distance on the bits, never by string equality** — string equality on a pHash is a
  strictly worse `sha256`. It is computed after EXIF orientation is applied, so a rotated
  re-import of the same shot hashes identically.

### Re-hashing is gated the same way re-extraction is

`hash_cache_key IS NOT cache_key` is the skip condition, mirroring `extract_log`. A photo edited
or replaced on the NAS is picked up on the next `--hash` pass automatically.

Regular syncs never touch the hash columns — they are deliberately absent from `sync_items`'
upsert SET list. Keep it that way, or every sync invalidates every hash.

An undecodable image still gets its `sha256`, with `phash` NULL. So `phash IS NULL` does **not**
mean "not yet hashed"; check `sha256 IS NULL` for that.

### Detection is NAS-free, deletion is not

`dedupe.py` is pure database reads over `photos`, with no `syno` or `pipeline` import — the same
boundary `cluster/` observes (ADR 01). `dedupe_writeback.py` is the only half that touches the
NAS, and the CLI wires the two together.

- `--exact` groups by `sha256`.
- `--visual` groups by `phash` hamming distance (`phash_hamming` in `sync/hashes.py`, using
  `int.bit_count()` on the XOR) via union-find, so near-duplicates chain transitively.
- Keep-rule per group (`_pick_keep`): highest `width*height`, then largest `filesize`, then lowest
  `id`.
- Both levels can run at once; `collect_drop_ids` de-dups the union so a photo is only deleted
  once.

### Deletion mirrors `apply`'s safety model

See ADR 05 for the full model. Specifically:

- **Dry-run by default.** `--apply` writes, with an interactive confirm unless `-y`.
- A `get_item` idempotency pre-check per id — already gone means skip.
- Every attempt is audited (`dedupe.delete` / `dryrun.delete`, reusing `apply.log`).
- `photos.deleted=1` is set locally on success.

Orphaned `faces`, `embeddings` and `syno_faces` rows are left in place, because every downstream
query already filters `deleted=0`.

## Consequences

- The underlying NAS call is `BackgroundTask.File.delete`, which returns "queued" rather than
  "done" (ADR 02). A successful `dedupe --apply` means the deletions were accepted, not that they
  have completed.
