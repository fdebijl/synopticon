# ADR 16 — Inspect: one photo, every box

**Status:** Accepted
**Applies to:** `web/inspect_routes.py`, `links.py` (`inspect_path`), `review/queries.py`'s `inspect_url`, `syno/foto.py::download_item_thumbnail`, `InspectView.vue`, `composables/usePanZoom.ts`, the review cards' photo link

## Context

Every number the pipeline produces about a photo — which detector fired, at what score, how the two
detectors' boxes merged, what MagFace thought of the crop's quality, which group the face landed in
— was reachable only by opening the database. When a face is missed, or a box looks wrong, or a
review item names a person nobody recognizes, the question is always the same: *what did we actually
see in that photo?* The review cards answered a different question. Their only photo link went
straight to Synology Photos, which shows Synology's view of the photo and none of ours.

Two things make that report harder to build than "SELECT and render":

- **The photo is not ours.** The web process holds no decoded images. Originals are a cache that
  `keep_originals = false` evicts, they can be HEIC (which no browser renders), and decoding one
  would mean `cv2` in the web process — which ADR 07 forbids.
- **The two sources of boxes do not share a coordinate system.** Our detections are pixel coords in
  the *EXIF-corrected* frame (`runner.load_image_bgr` runs `ImageOps.exif_transpose` before
  detection). Synology's `syno_faces` are normalized 0..1 against its own upright view.
  `photos.width`/`height` come from Synology's metadata and describe the *stored* frame, with
  `orientation` carried in a separate column — so on a rotated photo, the resolution we have on file
  is transposed relative to the boxes we drew.

## Decision

An in-app read-only page, `/inspect/{space}/{photo_id}`, backed by two routes.

### The image is a proxied NAS thumbnail, not our original

`GET /api/inspect/image/{space}/{photo_id}` fetches `Thumbnail.get` with `type="unit"` — the param
shape captured in `har/add_face_to_photo_without_faces.har`, so only the sizes that capture covers
(`sm`, `xl`) are accepted (ADR 02). It reuses QuickMerger's `NasSession` rather than building a
second logged-in client, which is why it is registered after QuickMerger in `create_app`, and it
buffers the body instead of streaming it — a `StreamingResponse` would hold the session lock for the
whole response. The result is always a JPEG, always aspect-preserving, and small enough to overlay.

A cached original is the fallback when the NAS call fails, so a report still renders offline for a
photo the pipeline recently processed. It is a fallback and not the primary source precisely because
it is often absent and sometimes undecodable by a browser.

Thumbnails are somebody's photos: `Cache-Control: private, max-age=86400`, and the path is in
`_NO_GZIP` (deflate over JPEG is loop CPU for nothing).

### Geometry is reconciled server-side, and the boxes get a vote

`display_size` picks the frame our detections were made against: Synology's resolution, swapped when
`orientation` is one of 5–8. That rule alone is not trustworthy — `orientation` is not always
populated, and cameras disagree — so both candidate frames are scored by *how many detections
actually fit inside them* and the better one wins. The boxes came out of the decoded image, so when
metadata and geometry disagree, the geometry is right. With no resolution at all, the extent of the
boxes themselves is the frame.

Every box then leaves the API as 0..1 of that frame, next to its raw pixel `bbox`. The SPA positions
plain absolutely-positioned elements by percentage, so it needs no image dimensions at all, and our
boxes and Synology's land in one space. When the served thumbnail's aspect ratio disagrees with the
frame anyway, the page says so rather than quietly drawing boxes in the wrong places.

### The stage is a pan/zoom viewport, and the overlay divides by the scale

A group photo on a laptop draws twenty boxes into a few hundred pixels, and their labels overlap
into an unreadable stack. `composables/usePanZoom.ts` turns the stage into a viewport: wheel and
trackpad pinch zoom around the pointer, two fingers pinch, drag pans, and a Maps-style
`+` / `−` / fit stack sits in the corner for the cases a gesture cannot reach (a phone that claimed
the swipe, a keyboard-only reader).

Three details are load-bearing:

- **The transform layer is the viewport's own size**, so a 0..1 box lands at `x * scale` percent of
  the viewport plus the pan offset in pixels, and the server-side normalization above is untouched.
  Translation is clamped to the scaled content, so the photo cannot be flung out of frame.
- **Only the photo is transformed.** The boxes sit in a sibling overlay that is never scaled, for
  two reasons. Chrome rasterizes a transformed layer once and scales the texture, so borders and
  labels drawn inside it return at 4× as a soft bitmap. And an overlay that *is* scaled inflates
  every border and label by the zoom factor, which recreates the overlap the zoom exists to escape.
  Unscaled, a 2px border stays 2px and a label stays legible while the boxes spread apart — the
  scaling is the point, the chrome is not. (`will-change: transform` is deliberately absent for the
  same reason: it pins the photo's raster at 1× too.)
- **The gesture never traps the reader.** A wheel-down at scale 1 is left to the page rather than
  swallowed as a no-op zoom-out, `touch-action` is `pan-y` until the reader zooms in, and the
  viewport takes no pointer capture — capture would retarget the click away from the boxes, which
  are `<button>`s. A drag past a few pixels sets a flag that suppresses the click that follows, so
  panning across a face does not select it.

Selecting a face while zoomed in re-centers on its box, which is what makes the face list below a
usable index into a crowded photo.

### Review rows are found by scanning, not by `json_extract`

`payload_json`'s face ids have no foreign key (ADR 15) and `json_extract` returns text on PostgreSQL
and an integer on SQLite, so no single comparison finds them on both backends. `_review_items` scans
`review_queue` and reuses `queries.payload_face_ids` — the same trade `orphaned_items` already makes,
acceptable here because Inspect is one photo at a time, off the hot path, and in a worker thread.

### In-app photo links point at Inspect; the NAS link moves inside it

`review_queue` items now carry `inspect_url` alongside `item_url`, and the review cards link to the
former. The two ids differ on purpose: `item_url` targets the similar-group top pick, because a
grouped member has no timeline route of its own, while Inspect reports on the photo that *owns the
faces* and so keeps the raw id. `links.py` builds both, keeping deep links in one place (ADR 01).

`item_url` stays as-is everywhere the link leaves the app — the standalone HTML report, job logs,
CLI output — since a relative SPA route means nothing there.

The id lives in the path rather than a query string so a report is a plain shareable link, and so a
future "inspect the next photo in this group" has somewhere to hang.

## Consequences

- Inspect is read-only in both directions and needs no consent gate. It is, however, the first
  read-path NAS call the web process makes on behalf of a page view: a NAS that is down degrades
  Inspect to a cached-original fallback or a 502, and never to a broken report — the JSON half is
  pure database work.
- The viewport is CSS transforms over one `<img>`; it never re-fetches at a higher resolution, so
  zooming past the thumbnail's own detail shows a soft photo with crisp boxes. That is the intended
  trade — the question is where the boxes are, not what the photo looks like.
- The overlay is only as good as `display_size`'s guess. The containment vote makes the common
  rotated-photo case self-correcting; a genuinely mislabelled thumbnail is surfaced to the reader as
  a warning rather than silently mis-drawn.
- Review cards no longer open Synology Photos in one click; it is two, via Inspect. That is the
  intended trade — the question a reviewer has in front of a wrong-looking card is nearly always
  about our own detection, not about Synology's copy of the photo.
