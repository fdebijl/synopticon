# ADR 15 — Review items whose faces no longer exist

**Status:** Accepted
**Applies to:** `review/queries.py` (`payload_face_ids`, `face_render_state`, `orphaned_items`, `orphan_counts`, `delete_items`), `pipeline/crops.py` (`_needs_backfill`, `_backfill_paths`), the `prune-queue` command and its `JOB_SPECS` entry, `/api/maintenance/counts`, `MaintenanceView.vue`

## Context

A production library reported ~1700 pending review items rendering as cards with **no face on them
at all** — a name, a confidence, and approve/reject buttons over empty space. `regen-crops` did not
bring them back.

The cause is a missing reference, not a missing image. `review_queue.payload_json` stores our
`faces.face_id` values inside JSON, with no foreign key — the schema cannot express one into a JSON
document, and nothing was ever added to compensate. Meanwhile `pipeline/runner.py::_process_photo`
begins a re-extraction with

```sql
DELETE FROM faces WHERE space = ? AND photo_id = ?
```

and re-inserts the detections, which take **new** ids: `faces.face_id` is
`INTEGER PRIMARY KEY AUTOINCREMENT`. So a `pipeline_version` bump silently invalidates every queue
row proposed before it. `queries.face_crops` builds its `face_id -> /crops/... URL` map from the
`faces` table, `ReviewCard.vue` renders the crop under `v-if="item.crop"`, and a lookup miss is
therefore not a broken image but no image element at all.

Two things follow that are worse than the cosmetic problem:

- **`regen-crops` cannot fix it, by construction.** A crop is rebuilt from the `faces` row's bbox
  and landmarks plus the original. When the row is gone, the inputs are gone. `_photos_with_faces`
  iterates *photos that have faces*, so an orphaned queue row is not even reachable from it.
- **An orphaned `approved` row still writes to the NAS.** `writeback.apply_reviewed` reads
  `photo_id`, `person_id`, `bbox_normalized` and `syno_face_id` out of the payload and never
  consults `faces` for an assign. The write succeeds against a bbox captured by a detector
  generation that has since been replaced — a correction the human could not see when approving it.

A second, narrower defect turned up while diagnosing the first. A crop is really *two* artifacts: the
images on disk and the `faces` row's `crop_path`/`ctx_crop_path` pointing at them. Only the columns
are read downstream. `regen_crops`' skip check (`_crops_present`) asked the **filesystem** whether a
photo was done, so a face whose files existed while its columns sat NULL was declared complete and
skipped on every pass — permanently invisible in review, and permanently unrepairable by the one
command whose job is to repair crops.

## Decision

Separate the two failures by what can still be recovered, and give each its own remedy.

### A referenced face is classified, not just looked up

`face_render_state` maps each `face_id` to one of three states, joining `faces` to `photos`:

| State | Meaning | Remedy |
|---|---|---|
| `ok` | `crop_path` is set | none needed; regen replaces a lost *file* |
| `repairable` | no `crop_path`, but the photo row is present and not deleted | `regen-crops` |
| `lost` | no `faces` row, or no crop and no original to rebuild from | `prune-queue` |

Ids with no row at all are reported as `lost` rather than omitted, so no caller has to distinguish
"absent" from "classified". The `photos` join is what makes `repairable` honest: without a live photo
there is nothing to fetch, so a crop-less face on a deleted photo is as lost as one with no row.

### An item is an orphan only when *every* face it names is lost

`payload_face_ids` reads all three payload shapes — a single `face_id`, a `face_ids` list, and
`evidence.exemplars`' `"space:person_id" -> [face_id]` map — and `orphaned_items` requires the whole
set to be lost. Partial survival is deliberately not an orphan: a merge whose exemplar list still
resolves in part renders thumbnails and stays judgeable. An item naming *no* faces is likewise left
alone; there is nothing to judge it on either way. Every payload read is defensive, because these
rows were written by pipeline versions that no longer exist.

### `prune-queue` deletes; it does not hide

Dropping the row is the point. The alternative — marking it `hidden` — is exactly wrong here:
`_existing_identities` counts `hidden` as seen (ADR 14), so hiding an orphan would suppress the
*correct* re-proposal forever. Deleting it lets the next `cluster` run re-propose the same decision
against the current faces, with a crop that renders.

Default scope is `pending` + `rejected`, the two statuses the next run re-proposes from scratch, so
pruning them loses no human input. `--include-approved` widens it to the decided statuses. That stays
opt-in rather than automatic because deleting an `approved` row discards a decision somebody made —
but the count is reported for *every* status regardless, since an approved orphan is a blind NAS
write and the only way that becomes visible is by being counted.

`audit_log.review_item_id` references `review_queue` with no cascade, so `delete_items` nulls the
link before deleting, in the same order `clear-queue` uses. The audit trail survives the row.

### `regen-crops` repairs the pairing, not just the pixels

The skip check now consults the row as well as the disk. A face whose files exist while its columns
are unset is repaired with a bare `UPDATE` — the paths are a pure function of `face_id`, so no
original is fetched, nothing is decoded, and no NAS traffic is generated. It is reported separately as
`backfilled`, because "repaired 4000 rows without touching the network" and "re-downloaded 4000
originals" deserve different numbers.

### The count is fingerprint-cached, and never fatal

`/api/maintenance/counts` parses every `review_queue` payload and then looks up the faces they name —
far too much work to redo per request (ADR 07: no per-request map derived from the whole library).
A wall-clock TTL is the wrong key, for the reason `review/lookups.py` already documents: the figure
moves the instant a `prune-queue` job deletes rows, and the Maintenance page reloads its counts right
after a job finishes, so a stale window would show the user a count their own action just corrected.

The key is instead an aggregate fingerprint over `review_queue`/`faces`/`photos` **plus the
`queue_counts` histogram the endpoint already computes**. The histogram is what catches a status
changing in place — approving an orphan moves it between statuses without altering any count or
extent, so a fingerprint over the tables alone would serve a stale answer. Like the crop disk-usage
figure it degrades to `{}` on any error rather than 500ing: an advisory count must never take the
Maintenance page down with it.

## Consequences

- **The schema still has no foreign key**, and adding one is not on the table: it would mean
  normalizing `payload_json` into a join table across six kinds with three different face-reference
  shapes, and the payload's whole value is that it is a self-contained snapshot of what was proposed.
  Detection is cheap and after-the-fact; the constraint would be neither.
- **A re-extract still orphans review rows.** This ADR gives the operator a way to see and clear
  them, not a way to avoid producing them. Making `run_extract` re-point payloads at the new ids
  would require matching old bboxes to new detections — the same fuzzy problem detection just
  re-answered, and a wrong match would silently retarget a human's decision.
- **`prune-queue` is not schedulable.** Repairing what a pipeline upgrade orphaned is a one-off, and
  `SCHEDULABLE` is deliberately a subset of what would validate (ADR 12).
- **Whether an orphan blocks its own replacement depends on the kind**, because
  `_existing_identities` keys each one differently. `assign`/`low_confidence`/`reassign` key on
  `(face_id, person_id)` and `new_person` on its sorted `face_ids` tuple — all renumbered by a
  re-extract, so the orphan does not match the re-proposal and `cluster` adds a fresh row beside it.
  That is why the queue *grows* with each version bump, which is the shape of the reported symptom.
  `merge`/`merge_named`, though, key on the `(person_a, person_b)` pair, which survives renumbering:
  an orphaned merge **suppresses** its own re-proposal, so that pair stays unjudgeable until the row
  is pruned. Pruning is the only way those come back.
