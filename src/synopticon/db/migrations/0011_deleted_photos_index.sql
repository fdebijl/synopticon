-- Migration 11: a partial index over the photos sync has marked deleted.
--
-- The review queue hides rows about deleted photos, so every review page asks
-- "which photos are deleted?". They are a sliver of the library; without this
-- that sliver costs a full scan of photos on each ask.

CREATE INDEX IF NOT EXISTS idx_photos_deleted ON photos (space, id) WHERE deleted = 1;
