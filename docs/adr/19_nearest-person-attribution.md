# ADR 19 — Attributing stranded faces to their nearest person

**Status:** Accepted
**Applies to:** `cluster/crossref.py` (`_nearest_person`, `load_known_persons`, `_crossref_core`'s `known_persons`), `eval/holdout.py`, `[crossref] nearest_person_sim` / `nearest_person_margin`

## Context

Crossref only suggests a face for a person through its cluster: the cluster needs `min_labeled`
tagged faces and a `majority` of them agreeing, and then every untagged member is proposed. A face
whose cluster maps to nobody — a **stranded face** — is never proposed for anyone.

On a 55.6k-face production library that was nearly all of them. Of 27.9k untagged faces, 654 sat in
a mapped cluster; the rest were singletons (12.4k), pairs (3k), or members of one 7.5k-face cluster
with 124 tagged faces and no majority. Untagged faces are small (median 75 px against 175 px for
tagged ones), which is why they end up alone.

Tuning did not reach them. Raising `edge_threshold` from 0.50 to 0.55 or 0.60 bought under two
points of recovery precision for 7–16 points of recall and moved 4–9k more faces into clusters of
one or two; the large cluster shrank mostly by losing its tagged members. HDBSCAN makes the same
trade more sharply, since its noise faces become singletons. Lowering `min_labeled` cannot map a
singleton at all.

Voting among a stranded face's kNN-graph neighbours was tried and rejected: 70–85% precision, and
most stranded faces have no tagged neighbour in their top 64.

## Decision

After cluster mapping, score each stranded untagged face against every person in its space as the
mean cosine similarity to that person's 5 closest tagged faces, and suggest the best person when the
score clears `nearest_person_sim` (0.55) and leads the runner-up by `nearest_person_margin` (0.08).

- Suggestions are always `low_confidence`: the per-face evidence has no cluster corroborating it.
  The payload carries `nearest_person.margin` and `runner_up_person_id`.
- The prediction feeds `predicted_person`, so `eval holdout` measures this step as well. As for
  clusters, it is recorded before the already-tagged-photo filter; the suggestion is not.
- At most one suggestion per (photo, person): two stranded faces in one photo are not both that person.
- **Only persons Synology lists are targets** (`known_persons`, live rows of `persons`). Synology keeps
  thousands of persons `list_persons` never returns — 4,974 on the library above, mostly holding one
  or two faces — and their faces label ours through `syno_faces`. With one face, a person's top-5
  mean is a single best match, so before this restriction 1,070 of 1,169 suggestions went to them.
- `nearest_person_sim = 1.0` switches the step off.

Measured on that library, with 20% of listed-person tags hidden: the step attributes 496 more
untagged faces to a listed person (654 → 1,150) for 485 suggestions, 121 of them to named persons.
Faces it recovered were correct 97% of the time (101 faces). Overall recovery precision moved from
99.83% to 99.76%.

## Consequences

- This is not a route to zero stranded faces. About 26.7k untagged faces stay unattributed; at a
  median 75 px most are probably bystanders, and no threshold should claim them.
- Faces held by unlisted persons still count as tagged, so they are neither stranded nor suggested.
  Treating those labels as absent would let ~1,800 of their 5.6k faces reach a listed person (from 59
  today), but those faces already exist on the NAS: they must be moved with `separate` (a
  `reassign`), never added with `add_face`, which would duplicate them. That change touches
  `reassign` generation and its apply gate, and is not part of this decision.
- The step costs one matrix product per space against all tagged faces — about a minute on the
  library above — and runs inside `_crossref_core`, so every `eval grid` cell pays it.
