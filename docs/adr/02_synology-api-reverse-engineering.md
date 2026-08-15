# ADR 02 — Treat captured browser traffic as the Synology API's ground truth

**Status:** Accepted
**Applies to:** `syno/`, `har/`, `sync/items.py`, `db/store.py::link_photo_id`

## Context

Synology Photos' Person API is undocumented. Community documentation exists but describes older
firmware: the live NAS runs `Browse.Item` v7 and `Browse.Person` v3 where the docs once described
v1/v2. Parameter encoding has quirks that fail *silently* — a malformed value produces a
successful-looking response that did nothing.

Guessing at payload shapes therefore produces write operations that appear to succeed and do not,
which is the worst possible failure mode for a tool whose entire purpose is writing corrections
back into someone's photo library.

## Decision

Discover API versions at runtime, centralize the encoding quirks in one function, and derive
every write payload byte-exactly from captured browser traffic.

### Never hardcode API versions

`client.py` discovers min/max per API at runtime via `SYNO.API.Info` and caches the result in
`sync_state`. Hardcoding a version pins the client to whatever firmware the author happened to
be running.

### Parameter encoding is centralized

`client.encode_params` holds the Synology-specific quirks: lists and dicts become compact JSON
strings (`id=[2660]`), and some string values must be quoted — wrap those in `QuotedString`.
Getting this wrong fails silently, which is why it is heavily unit-tested.

### Write throttling is auto-detected

`WRITE_METHODS` (a frozenset in `client.py`: `add_face`, `merge`, `set`, `delete_face`,
`separate`, `upload`, `delete`) selects a separate, slower token bucket than reads. Method name
drives the choice, so a new write method is throttled by adding it to the set.

### HAR captures are the ground truth for payloads

Sample captures live in `./har/` (the directory is gitignored beyond those samples):

| Capture | Covers |
|---|---|
| `add_face_to_photo_without_faces.har` | the `Person.add_face` v3 → `Upload.Face.upload` multipart → `list_face` verify chain |
| `reassign_existing_face_to_other_person.har` | `separate` |
| `remove_face_from_photo.har` | `delete_face` |
| `deleting_one_photo.har`, `deleting_multiple_photos.har` | photo deletion, used by `dedupe` |

Consult those before changing payload shapes.

## Verified API semantics

These were established from the captures and are easy to get wrong from the names alone:

- **`separate` v1** (`face_id=[..]`, `target_id`, quoted `name`) atomically moves an existing face
  to another person. The face keeps its `face_id`. This is what the `reassign` review kind uses,
  and it is reversible.
- **`delete_face` v1** (`face_id=[..]`, `person_id`) **hard-deletes** the face detection from the
  photo. It is not a reversible unassign.
- **`show` v1** (`id=[..]`, `show` bool) only flips the People-view visibility flag. Reversible,
  faces untouched. It is in `WRITE_METHODS` and exposed as `SynoWriter.set_show`.
- **Photo deletion is a different API from `delete_face`**: `BackgroundTask.File.delete` v1
  (`item_id=[..]` batch, `folder_id=[]`) queues an async background task and returns `task_info`
  with status `waiting`. `success:true` means *queued*, not *done* — no status-poll was captured.
  Whether it recycle-bins or hard-deletes is not determinable from the captures; verify against a
  live NAS before relying on either.

## Mirrored namespaces

`SYNO.Foto.*` (personal space) and `SYNO.FotoTeam.*` (shared space) are mirrored APIs. Every
database row carries a `space` column and `client.api_name(space, suffix)` picks the namespace.

## Similar photo groups (stacking)

A deep link to a non-top-pick member of a Synology "similar photo group" redirects to the
homepage — the grouped timeline only surfaces the group's `top_pick` item. Getting a working deep
link therefore requires knowing group membership.

`Browse.SimilarItem` (v1–2, both namespaces) is the only source of it. Two alternatives were
tried and do not exist: `additional=["similar"]` on `Browse.Item.get` is invalid (error 120), and
`Browse.Similar` does not exist (error 103).

`SimilarItem.list` paginates like `Browse.Item.list`. Only the top-pick row of each group carries
a top-level `similar` key (`{id, count, top_pick, item_id: [...]}`, a sibling of `id`/`filename`);
non-top-pick members are omitted from the response entirely.

`sync/items.py::sync_similar` records this as `photos.similar_top_pick`, set for every group
member including the top pick and NULL when ungrouped. `db/store.py::link_photo_id` resolves a
photo id through it, and every deep-link build site calls it (`review/queries.py`, `cli.py`'s
dedupe report, `sync/persons.py`'s face-skip logging, `pipeline/runner.py`'s extract-skip
logging), so links always target the visible group cover.

## Consequences

- A firmware upgrade that changes payload shapes requires a fresh capture, not a code guess.
- Tests never contact a real NAS; all HTTP is mocked with respx. Live verification is manual, via
  `synopticon check` and read-only `sync`.
