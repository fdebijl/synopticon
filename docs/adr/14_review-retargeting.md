# ADR 14 — Correcting a suggestion instead of rejecting it

**Status:** Accepted
**Applies to:** `review/queries.py` (`retarget_item`, `person_search`, `_DECISIONS`), `cluster/crossref.py::_existing_identities`, the `/api/review/{id}/retarget` + `/api/review/persons` routes, `ReviewCard.vue` / `PersonPickerDialog.vue`

## Context

The review queue was a two-answer interface: approve what the pipeline proposed, or reject it. Both
answers throw away the interesting case — the pipeline found a real face group and named the wrong
person for it. Rejecting records "not this", never "it's her", so the same photo comes back
unlabelled and the next grouping run proposes the same wrong person again.

Two specific dead ends motivated this:

- **`new_person` was unwritable.** `apply_reviewed` has no branch for the kind and `syno/` has no
  `create_person` wrapper (`Browse.Person` has no captured create call — ADR 02), so approving a
  suggested new person wrote nothing to the NAS. The queue offered a decision with no effect.
- **Rejection is not sticky, deliberately.** `_existing_identities` dedupes against
  `pending/approved/applied` only, so a rejected row is re-proposed on the next run. That is wanted
  for "not right now" but useless for "this cluster is a stranger in the background, stop asking".

## Decision

Three per-card actions — Hide, Merge into…, Reassign… — implemented **entirely inside
`review_queue`**. None of them touches the NAS.

### Review stays read-only toward the NAS

The write still happens later, through `apply`, under the tier flags of ADR 05. That is the whole
reason these actions need no `confirm: true` gate, no `SynoWriter`, no pre-write ground-truth
re-read, and no answer to "how can this not perform a named merge?" — they cannot write anything.
QuickMerger remains the single GUI surface that writes outside `apply`.

The alternative — Hide calling `set_show(person_id, False)` like QuickMerger's Hide does — is not
merely riskier, it is unreachable: a `new_person` payload is `{face_ids, size, suggested_name}` with
no Synology person behind it. These are faces Synology never grouped. There is nothing there to
hide.

### `hidden` is a status, and it is sticky

`_DECISIONS` gained `hide -> hidden`, so Hide rides the existing decide endpoint and `undo_decision`
un-hides for free (`_UNDOABLE` gained `hidden`). `review_queue.status` has no CHECK constraint, so
this needed no migration.

`_existing_identities` now selects `pending/approved/applied/hidden`. That one word is the entire
difference between hiding and rejecting, and it is the reason both exist:

| | `apply` writes it | Re-proposed next run | Undoable |
|---|---|---|---|
| `rejected` | no | **yes** — "not right now" | yes |
| `hidden` | no | **no** — "never again" | yes |

### Retargeting rewrites the payload, and auto-approves

`payload_json` is where the target person lives — there is no `person_id` column — so a retarget is
a payload rewrite, following `set_suggested_name`'s precedent. Two shapes:

- **`assign` / `low_confidence`** — rewrite `person_id`/`person_name`, keep the overruled id as
  `original_person_id`, mark `manual_target`, and **clear `confidence`**. The stored score was a
  similarity to the person the human just overruled; leaving it in place would read as an
  endorsement of the new one. Cross-space retargets are refused (`SpaceMismatch` → 422): a face
  cannot move between the personal and shared namespaces.
- **`new_person`** — expand the cluster into one `assign` row per face against the chosen person,
  then retire the original row as `hidden` with a `retargeted_to` breadcrumb. `assign` is already
  the reversible "tag this face as P" tier, already applied, already deduped — giving `new_person` a
  write path of its own would have meant a new NAS call and a new tier for no extra capability.

Membership comes from `cluster_members` (seeded from an exemplar, since the payload carries neither
the cluster id nor more than 20 face ids), falling back to the stored exemplars when `run_id` is
NULL. Merging a 60-face cluster from its 20 stored exemplars would silently leave two thirds of it
untagged.

Rows land **`approved`**, not `pending`: picking the person in the dialog *is* the decision, and
`apply` is still the gate that writes. Requiring a second `y` afterwards would make every
correction a two-step and put the card back in a queue the human just finished with.

`original_person_id` is also registered in `_existing_identities` — after the rewrite the overruled
`(face_id, person_id)` pair exists in no payload, so without that the next run would helpfully
propose the wrong person again alongside the correction.

### The person picker reads the local mirror

`GET /api/review/persons` searches the local `persons` table, not `foto.suggest_person`. Review is
otherwise a pure local-DB surface that works with the NAS unplugged, and the local answer carries
the same `/crops/...` URLs the cards already show, so the dialog's target side matches the card's
thumbnails instead of QuickMerger's NAS-proxied ones. Only named people are offered — there is no
name to search an unnamed person by, and naming those is what QuickMerger is for.

## Consequences

- A GUI click now creates `status='approved'` rows directly. Safe because the tier flags on `apply`
  are still the only thing that writes to the NAS — but it means "approved" no longer implies "a
  pipeline proposal a human said yes to"; it can mean "a target a human chose". `manual_target` is
  what distinguishes them, and it is why such a card shows "you picked this person" where an
  ordinary assign shows a confidence.
- Hide is undoable; a retarget is not, and both halves of that are enforced in `queries.py` rather
  than by UI convention: `retarget_item` refuses an `applied` or `hidden` row (which is also what
  stops a repeated call from expanding the same cluster twice), and `undo_decision` refuses to
  un-hide a row carrying a `retargeted_to` breadcrumb, since its faces already sit in approved
  `assign` rows. The breadcrumb records what the row became; the reverse operation is left unbuilt
  rather than half-built.
- `hidden` is a fourth terminal-ish status the UI must account for: it is in the status filter, and
  the dashboard counts it as reviewed work.
- `clear-queue` deletes only `pending` rows, so hidden decisions survive it — consistent with the
  rest of the queue's decided states.
