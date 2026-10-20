# ADR 20 — Unlisted persons never a target; wrong and unlisted tags reassigned per face

**Status:** Accepted
**Applies to:** `cluster/crossref.py` (`_crossref_core`'s mapping, `ClusterInfo.listed_total`, `_nearest_person`'s reassign branch), `[crossref] majority` / `min_labeled` / `nearest_person_*` help text
**Builds on:** ADR 19

## Context

Synology keeps far more persons than `list_persons` returns. On a 64.4k-face library it listed 437 and
held about 5k more, mostly with one or two faces, which still tag 6,542 of our faces through
`syno_faces`. ADR 19 stopped the nearest-person step from targeting them; cluster mapping did not:

- **Unlisted persons were mapping targets.** 115 of 713 mapped clusters mapped to an unlisted (or
  deleted) person, so crossref suggested faces *for* them. 98 such assigns and 11 reassigns into
  them had been applied — moving faces into exactly the dust this work is meant to clear.
- **They diluted real majorities.** Each unlisted person is a separate vote, so 31 clusters holding a
  listed person's faces (395 untagged among them) fell under `majority` and mapped to nobody.
- **Their faces counted as tagged**, so neither the assign path nor ADR 19's step proposed them, and
  the cluster-path reassign rarely fired: its confidence is the mean similarity to *every* tagged
  face of the target in the cluster, which sinks below `assign_sim` in a large cluster. 2,521 of
  them sat in mapped clusters; 84 were proposed.

`separate` is the right write for them — the face already exists on the NAS, so `add_face` would
duplicate it — and it is proven on unlisted persons: 355 reassigns out of one had been applied.
Merges are proven too: 196 of 197 applied merges involving an unlisted person came from the
intra-cluster trigger.

## Decision

- **Mapping counts listed persons only** (`ClusterInfo.listed_total`); an unlisted person can never be
  a cluster's target, assign target or reassign target. `labeled_total` and `votes` keep every tag,
  so the intra-cluster merge — the bulk route for an unlisted person's faces — is unchanged.
- **The nearest-person step also scores tagged faces** (SCRFD-confirmed, with a Synology face id, in
  the same space as their tag), never counting the face as evidence for itself. When the best listed
  person is not the face's Synology person and clears `nearest_person_sim` and
  `nearest_person_margin` — where the runner-up may be the Synology person — it queues a `reassign`.
  This covers faces held by unlisted persons and plainly wrong tags on listed ones. It is skipped
  when the face's cluster maps to its Synology person (two signals already agree), and when the
  cluster pass proposed a reassign for it already.
- **A target needs `min_labeled` tagged faces**, for assigns and reassigns alike. Smaller persons stay
  in the scoring, so they still count as runner-up.
- Reassigns keep their gate, `--apply --apply-reassigns`; nothing about the safety model changes.

Measured on that library against the previous release, with 20% of listed tags hidden:

| | Before | After |
|---|---|---|
| Suggestions targeting an unlisted person | 115 mapped clusters' worth | 0 |
| Reassigns | 86 | 1,460 (1,432 out of unlisted persons, 28 out of listed ones) |
| Merges | 97 | 99 |
| Untagged faces attributed | 1,635 | 1,656 |
| Hidden tags recovered / correct | 93.8% / 99.77% | 93.4% / 99.81% |

Visual review of random samples: reassigns out of unlisted persons through a cluster were almost all
right; through the nearest-person step most were, a few uncertain. The 28 out of listed persons
included one known-wrong tag correctly moved (a photo the person cannot appear in), and several
look-alikes within one family (siblings, a parent and child) that only a human can settle. No
threshold on the gap to the current person separates those: every one of them has a gap of 0.24 or
more.

### Rejected: splitting superclusters

The two unmapped clusters over 100 faces held 9,028 faces. Re-clustering only them, stepping the
edge threshold up by 0.05 to 0.70 until each piece mapped or fell below 100, broke them into 6,838
pieces, 5 more attributed faces and 16 more `new_person` proposals — a gorilla statue, a game cover,
an out-of-focus blur, the backs of heads, a painting. 66% of their faces are YOLO-only detections and
almost none are tagged (2 of the holdout's hidden tags were inside), so there is nothing to map them
to. Their unlisted-person faces (143) are reached by the per-face step instead, wherever they sit.

## Consequences

- Reassign volume is now dominated by Synology's own leftovers, and reviewing 1,400 rows is the cost.
  Each is a reversible `separate`, gated, and pre-checked against the NAS before writing.
- `clusters.vote_fraction` is now the share of *listed* votes; `clusters.labeled_count` still counts
  every tag.
- `new_person` still requires no tag of any kind, so a cluster of only unlisted faces is not proposed
  as new: those faces already exist on the NAS.
