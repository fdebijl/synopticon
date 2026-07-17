-- Migration 4: index sha256 for fast exact-duplicate grouping (`dedupe --exact`).
--
-- Exact-dup detection groups photos by sha256; without an index that's a full
-- table scan on every run. Visual dedup (phash hamming distance) stays a scan
-- either way -- bit distance isn't indexable here.

CREATE INDEX IF NOT EXISTS idx_photos_sha256 ON photos (sha256);
