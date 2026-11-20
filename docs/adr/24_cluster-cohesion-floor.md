# ADR 24 — A cluster too loose to be one person maps to nobody

**Status:** Accepted
**Applies to:** `cluster/crossref.py` (`_crossref_core`'s mapping, `ClusterInfo.cohesion`), `clusters.cohesion`, `[crossref] min_cohesion`, `web/mcp/tools_diag.py::_why_unmapped`
**Builds on:** ADR 19, ADR 20

## Context

Cluster mapping asked one question: do the tagged faces in this cluster agree (`majority`,
`min_labeled`)? It never asked whether the cluster is a person at all.

Chinese Whispers chains small, blurry and half-hidden detections into catch-all clusters. ADR 20
measured two of them (9,028 faces) and rejected splitting them; they were harmless because no
tagged majority formed inside them. On an 82.6k-face library they had grown to 7,017 and 7,873
faces, and the first one tipped: 13 of its faces were tagged, 8 of those to listed persons, 5 of
those to one person. 5/8 = 0.625 cleared `majority`, the cluster mapped, and all 7,005 untagged
members were queued as `low_confidence` for that person — a mapping resting on 0.2% of the cluster.

The person was the one the operator files bad faces under, which is what makes this
self-reinforcing: bad faces live in the catch-all clusters, so every one tagged that way is another
vote there.

## Decision

A cluster maps only when it also clears `min_cohesion`: the mean pairwise cosine similarity of its
fused embeddings, default 0.38. The value is stored as `clusters.cohesion` so a diagnostic can say
why a cluster with a majority did not map. `vote_fraction` is still recorded for such a cluster.

Measured on that library (run of 27,356 clusters, 714 mapped; 96 mapped clusters sampled, including
every one over 200 faces):

| | Cohesion |
|---|---|
| Catch-all cluster that mapped (7,017 faces) | 0.337 |
| Its unmapped twin (7,873 faces) | 0.362 |
| Lowest real mapped cluster (289 faces) | 0.408 |
| Real mapped clusters, 100+ faces: p5 / median | 0.418 / 0.477 |
| Real mapped clusters, 3-9 faces: min / median | 0.469 / 0.557 |

At 0.38 neither catch-all maps and no sampled real cluster is lost; at 0.42 four real clusters
holding 2,857 faces would be. The margin is thin on both sides (0.018 and 0.028), which is why it is
a setting. Cohesion falls with size, so the clusters closest to the floor are a heavily photographed
person's main one.

Nothing is stranded by the refusal: untagged faces in an unmapped cluster are probes for the
nearest-person step (ADR 19), which judges each on its own evidence.

### Rejected

- **Excluding the person as a target.** They are a real person with a clean cluster of their own
  (184 faces, unanimous), and the next catch-all majority would simply belong to someone else.
- **A floor on the share of tagged faces.** It separates far more widely here (0.2% and 2% against
  a real minimum of 37%), but it punishes the case assigns exist for: a known person with a few
  tags in a large untagged cluster. It would also be a property of how thoroughly a library is
  tagged, not of the cluster.
- **Detector mix.** One real cluster of 809 faces is entirely YOLO-only, and 28 of the 95 sampled
  real clusters have almost no detector-confirmed faces.
- **Median or p5 similarity to the centroid.** The median carries the same information as cohesion
  with no better margin; p5 does not separate at all.

## Consequences

- The floor lives in the embedding space `edge_threshold` does. Changing `fusion_weights` or the
  embedders moves every cluster's cohesion, and the default has to be re-measured;
  `grouping_summary` reports the quantiles for mapped clusters.
- Cluster-path reassigns and cluster-pair merges are gated by the same mapping, so they stop for a
  refused cluster too. The intra-cluster merge is not, and is unchanged.
- The catch-all clusters still exist. This makes them inert; it does not dissolve them.
- Runs made before migration 15 have a NULL `cohesion`.
