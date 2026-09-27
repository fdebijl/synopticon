# ADR 18 — Review items about photos the NAS no longer has

**Status:** Accepted
**Applies to:** `cluster/graph.py::load_fused`, `review/queries.py` (`voided_items`, `_not_voided`, `load_review_items`, `count_review_items`, `queue_counts`, `bulk_approve`), `review/lookups.py::VoidedCache`, `syno/writeback.py::apply_reviewed`, migration `0011_deleted_photos_index.sql`

## Context

An approved assign turned up in review with a card that led, through Inspect, to a photo marked
"deleted on the NAS". Nothing between `sync` and the review page knew about deletion:

- `sync/items.py` marks a vanished photo `photos.deleted = 1` and leaves its `faces` and
  `embeddings` rows alone. A photo that reappears on a later sync is flipped back to `0`.
- `cluster/graph.py::load_fused` read every `variant='orig'` embedding, so faces on deleted photos
  were clustered, cross-referenced and proposed like any other.
- The review queries read `review_queue` without looking at `photos`.
- `writeback.apply_reviewed` pre-checks with `list_item_faces`. For a deleted photo that raises
  `SynoApiError`, which the pre-check treats as "not applied yet", so the write went ahead, failed,
  and counted toward the circuit breaker. A handful of them could stop a real apply run partway.

ADR 15 already treats a deleted photo as unrecoverable, but only as a crop source (`FACE_REPAIRABLE`
requires a live photo). It never asked whether the proposal itself still meant anything.

## Decision

### Clustering skips faces on deleted photos

`load_fused` excludes a face whose photo row is marked deleted. A face with *no* photo row at all
is kept, because that is not evidence of deletion. The graph cache is keyed on the face-id set, so
the next run rebuilds rather than serving a graph containing the deleted faces.

### A row about a deleted photo is *voided*: filtered at read time, never written

`queries.voided_items` names the rows, in any status, that are about a deleted photo:

| Payload | Void when |
|---|---|
| names a photo (`space` + `photo_id`: assign, low_confidence, reassign) | that photo is deleted |
| names only faces (`new_person`, `restore_disagreement`) | every face it names is on a deleted photo |
| `merge` / `merge_named` | never. It is about two people, and a deleted exemplar photo only thins the evidence |

The list, the item count, `queue_counts` (the review tabs, the Apply page's "N approved" badges, the
dashboard and Maintenance) and `bulk_approve` all exclude them. `bulk_approve` has to: an approval
of a row nobody could see is a decision nobody made.

**Filtering, not pruning or hiding, is the point.** `deleted` is reversible: a photo that comes back
brings its rows back with it, still carrying whatever a human decided. Pruning would throw away
approved decisions, which ADR 15 only does on explicit opt-in. `hidden` would be a decision nobody
made, and `_existing_identities` would count it as seen forever (ADR 14).

### Apply skips voided writes, and leaves the row approved

`apply_reviewed` skips an assign or reassign whose photo is marked deleted. It logs the skip and
counts it under `skipped`, not `failed`. The row keeps `status='approved'`, for the same
reversibility reason. A dry run makes the same skip, so the preview's numbers match.

### The ids are computed in Python and cached per queue shape

`json_extract` is translated for DDL only (ADR 09), and it returns text on PostgreSQL and an
integer on SQLite, so no single SQL comparison against `payload_json` holds on both. `voided_items`
therefore parses payloads the way `orphaned_items` does, and the filter inlines the resulting ids
as `item_id NOT IN (...)`. Binding them could overflow SQLite's parameter ceiling, and `int()`
makes each one safe to inline.

The common case, no deleted photos, is one query answered from the partial index
`idx_photos_deleted` (migration 11). Otherwise it is a queue scan, which ADR 07 does not allow per
request. `VoidedCache` holds the set, keyed on the deleted photos' count and id sum, `faces`'
count and max id, and `review_queue`'s count and max id. Unlike `LookupCache` it has to move when
the queue grows, since a new row may be void, but a decision only flips a status and leaves it be.
The web process owns one instance and passes it to the review routes, `gather_stats` and the
Maintenance counts. Other callers (the legacy review app, the CLI) compute the set directly.

## Consequences

- **"Deleted" means "deleted as of the last sync."** A photo removed on the NAS since then still
  shows until the next sync, and apply's own pre-check is still the last line of defence.
- **Voided rows are invisible, not gone.** There is no count of them in the UI. They cost nothing
  while hidden, and pruning them is unnecessary: a pending one is simply never seen, and if its
  photo returns it is exactly the right proposal to show.
- **Maintenance's queue histogram excludes voided rows too**, so it agrees with the review tabs. The
  orphan count (ADR 15) is computed separately over every row and is unaffected.
- **Per-face deletion inside a merge or new_person is not reflected.** A `new_person` card with some
  faces on deleted photos still renders, and those faces' crops still show. When a retarget turns
  such a card into assigns, the assigns on deleted photos are voided individually.
