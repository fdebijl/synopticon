-- Migration 5: split named->named merges into a distinct, more dangerous kind.
--
-- Merging two already-named people is irreversible and destroys a human-assigned
-- label, so it now lands in review_queue as kind='merge_named' (generated fresh
-- by crossref and gated separately at apply time). Reclassify any existing
-- un-applied merge rows whose *both* sides carry a non-empty name so old queues
-- get the stricter gate too. Applied/rejected rows are historical and left as-is.

UPDATE review_queue
SET kind = 'merge_named'
WHERE kind = 'merge'
  AND status IN ('pending', 'approved')
  AND trim(coalesce(json_extract(payload_json, '$.person_a.name'), '')) <> ''
  AND trim(coalesce(json_extract(payload_json, '$.person_b.name'), '')) <> '';
