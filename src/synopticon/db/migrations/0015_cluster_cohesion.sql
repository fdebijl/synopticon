-- Migration 15: record each cluster's cohesion.
--
-- The mean pairwise cosine similarity of a cluster's fused embeddings, as
-- crossref measured it when deciding whether the cluster may map to a person
-- ([crossref] min_cohesion). NULL for a singleton and for runs made before
-- this migration.

ALTER TABLE clusters ADD COLUMN cohesion REAL;
