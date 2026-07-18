-- Migration 7: record Synology's "similar photo" (stack) grouping.
--
-- Synology Photos groups near-duplicate shots into a "similar photo group" and
-- the grouped timeline only ever surfaces the group's top_pick item -- a deep
-- link to any non-top-pick member 404s to the homepage. similar_top_pick lets
-- link-building code substitute the group's top_pick id so links always
-- resolve. NULL means the photo is ungrouped. Populated by `sync`'s similar
-- pass (sync/items.py::sync_similar) from SYNO.Foto(Team).Browse.SimilarItem;
-- set for every member of a group, including the top pick itself.

ALTER TABLE photos ADD COLUMN similar_top_pick INTEGER;
