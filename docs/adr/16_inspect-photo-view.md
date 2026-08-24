# ADR 16 — Inspect: one photo, every box

**Status:** Accepted
**Applies to:** `web/inspect_routes.py` (`frame_candidates`, `resolve_frame`, `rotate_box`), `links.py` (`inspect_path`), `review/queries.py`'s `inspect_url` / `box_iou` / `photo_syno_match` / `assign_face`, `syno/foto.py::download_item_thumbnail`, `InspectView.vue`, `PersonPickerDialog.vue`, `composables/usePanZoom.ts`, the review cards' photo link

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

An in-app page, `/inspect/{space}/{photo_id}`, backed by three routes — two that read and one
that writes the review queue, never the NAS.

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

There are only ever two candidate frames — Synology's stored resolution and its transpose — and
`frame_candidates` returns them as an *order*, not an answer.

**`orientation` gets no vote, and that is a measured result, not a preference.** The rule used to be
"swap when `orientation` is one of 5–8", on the reasoning that EXIF 5–8 mean the corrected frame is
rotated. Checked against Synology's own face boxes over a 13k-photo library, that swap was wrong on
**2236 of 2485** rotated photos. Synology reports `width`/`height` for the frame it *displays* and
hands over the EXIF tag beside it, so the swap was correcting a pair that had already been corrected.
Every other reader of those columns — `crossref._load_face_meta`, `queries._face_targets` — has
always taken them unswapped, and clustering works; this was the one place that did not. Dropping the
rule took the seed from 90% wrong to 99% right on the same library.

What survives is the containment count: a box outside a frame cannot have come from it, so where it
fires it is proof rather than a claim, and it may reorder the pair. It only fires when a face sits
past the shorter side, which on a photo with faces near the middle it never does. With no resolution
at all, the extent of the boxes themselves is the frame.

Every box then leaves the API as 0..1 of that frame, next to its raw pixel `bbox`. The SPA positions
plain absolutely-positioned elements by percentage, so it needs no image dimensions at all, and our
boxes and Synology's land in one space.

### Two faults look alike, and one search settles both

An overlay can be wrong in two ways, and only one of them is a rotation:

- **The wrong divisor.** The seed is the transpose of the truth, so the same pixel boxes were divided
  by a swapped pair. Nothing is turned; the boxes squash along one axis and slide toward a corner.
  Re-normalizing is the only fix — a rotation makes it worse. This is the fault photo 60251 hit.
- **A real quarter-turn**, where our EXIF-corrected frame and Synology's upright one genuinely
  disagree about which way is up.

`resolve_frame` searches both axes at once: every (frame, turn) pair scored by how well our boxes
land on Synology's, winner takes it. Synology's boxes are the arbiter because they are the only
geometry here not derived from the columns already under suspicion.

Three details are load-bearing:

- **The score is a mean IoU over *Synology's* boxes, not ours.** Synology labels a fraction of the
  faces we find — photo 60251 has one of seven — so averaging over ours buries a perfect match under
  the faces it never named. Each of their boxes asks the same question, and every one should be able
  to answer it.
- **A correction must be clearly better, not merely better.** A face near the middle of a photo
  nearly overlaps itself under a half-turn, so "best wins" lets symmetry flip frames that were
  already right: on the real library a bare `>` invented a turn on 99 photos, against 1 with the
  margin. Hence `_MIN_MARGIN` over the seed and `_MIN_FIT` in absolute terms.
- **"The seed stood" and "nothing was checked" are different answers**, and `rotation_source` says
  which. A photo Synology has no faces on cannot be checked at all, which is common enough that
  claiming otherwise would be the misleading part.

The rotation is **reported, not applied**: `display.rotation` is a quarter-turn clockwise and the SPA
applies it to our boxes only, since Synology's are already in the served photo's frame. `box` keeps
meaning what it always has — 0..1 of the frame we detected in — so nothing downstream has to know the
correction exists.

**The photo is never the thing that turns.** Rotating it would leave a reader tilting their head to
check a box, and because the stage takes its aspect from the image (`.ins-img` is `width: 100%`), the
overlay would have to be re-based on a frame the layout does not have. Turning the boxes costs four
arithmetic cases and leaves the picture readable.

### The browser holds the one measurement the server cannot take

`resolve_frame` needs Synology faces to vote with, and plenty of photos have none. For those the
browser has evidence nobody else does: the shape of the photo that actually arrived. A frame whose
aspect is the *transpose* of the served image's is the wrong-divisor fault — both frames describe the
same upright photo, so they cannot legitimately disagree about which side is longer unless the
content really is turned, and a real turn is what the server would have found evidence for. So the
SPA re-normalizes (`reframe`, a rescale by the ratio of the frames' sides — the pixels never moved,
only the numbers they were divided by).

It never overrules a frame Synology's boxes chose. That gate is what stops the heuristic from undoing
a real answer, and it is why `rotation_source` is on the wire at all.

### There is no manual override, and the measurement is why

An early cut carried a **Rotate boxes** button for whatever the automatic passes missed. The numbers
retired it. Over the same library, of the photos that can be checked at all: 99.30% seeds confirmed,
**0.61% needed a frame swap**, **0.008% needed a turn** — one photo, which had Synology faces and so
was settled without help. What a manual turn could uniquely rescue is a photo with no Synology faces
*and* genuine rotation: bounded by that 0.008%, or well under one photo across the 5540 unchecked.

Against nothing, it cost something. A reader meeting misaligned boxes reaches for the only control
on offer, and 75 times out of 76 the fault is the frame, which no turn fixes — the first version of
the alignment note said exactly that, and was wrong. It also composed badly: with the SPA re-framing
and the reader then turning, boxes go through `rotate(reframe(box))`, which is neither correction.

So the note explains and does not offer. It distinguishes a re-framed photo, a turned one, a
transposed pair with no evidence to settle it, and a mismatch that is neither a crop nor a turn. When
the residual case does turn up it renders wrong and says so, which is the honest end state for
something this rare — better than a control that is the wrong answer to the question readers will
actually be asking when they find it.

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

### Tagging a face is a queue write, and Inspect picks the tier

The report answers "what did we see", and the next question a reader always has is "then say it
*is* her". The queue could not: nothing proposes a face the pipeline never grouped, or one whose
group mapped to nobody, so there was no card to correct. A per-face **Tag as… / Reassign…** button
opens the same `PersonPickerDialog` the review cards use and writes one `review_queue` row.

It needs no consent gate for ADR 14's reason: the row is queue-only, and `apply` still holds every
flag that reaches the NAS. What is new is that **the tier is decided server-side, from geometry**.
`queries.photo_syno_match` pairs our detections with Synology's boxes on that photo — greedy, best
pair first, at the same IoU 0.3 `crossref` matches ground truth at, so Inspect and clustering never
disagree about whose box is whose — and the pairing chooses the kind:

| Synology's box over this face | Kind written | `apply` flag |
|---|---|---|
| none, or one it never named | `assign` | `--apply` |
| named as somebody else | `reassign` | `--apply --apply-reassigns` |
| named as the person just picked | *refused, 409* | — |

Letting the client name the kind would have been a way to write a `reassign` under the `assign`
flag, which is exactly the tier boundary ADR 05 exists to hold. The refusal is not a courtesy
either: `assign` calls `add_face`, so tagging a face Synology already has on that person adds a
second box for the same face rather than doing nothing.

The row lands `approved` with `manual_target`, `confidence` NULL and `source: "inspect"` — a human
picking the person *is* the decision (ADR 14), and there is no pipeline score to carry. Any queued
row speaking for **this face alone** is superseded to `hidden`, carrying a `superseded_by`
breadcrumb: two approved rows for one face would tag two people onto the photo, and `hidden` (not
deleted) keeps the overruled `(face_id, person_id)` pair registered in `_existing_identities` so the
next grouping run does not helpfully propose it again. A row naming *more* than this face — a
`new_person` cluster, a merge's exemplars — is left alone; it is a claim about a group, and `apply`
cannot write it into a competing tag anyway.

The picker itself stopped taking a `ClientReviewItem` and now takes a described source (a label,
some thumbnails, the space the target must share, an optional note). That is what lets Inspect point
it at a bare face without inventing a queue row to hold one, and it keeps the person search, the
local-mirror rule and the keyboard handling in one component rather than two.

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

- Inspect writes nothing to the NAS and so needs no consent gate, but it is no longer read-only
  toward the database: a click can create an approved `reassign` row, the tier that destroys nothing
  but does move a human-visible label. It stays behind `--apply-reassigns` — Inspect chooses the
  tier, it does not spend it.
- It is the first read-path NAS call the web process makes on behalf of a page view: a NAS that is
  down degrades Inspect to a cached-original fallback or a 502, and never to a broken report — the
  JSON half is pure database work.
- The face↔Synology pairing now runs on every report, not just on the tag. That is deliberate: the
  button has to name the tier before the reader commits to it, and a reader looking at a box wants
  to know whether Synology saw the same face anyway.
- The viewport is CSS transforms over one `<img>`; it never re-fetches at a higher resolution, so
  zooming past the thumbnail's own detail shows a soft photo with crisp boxes. That is the intended
  trade — the question is where the boxes are, not what the photo looks like.
- On the library this was measured against, `resolve_frame` confirms the seed on 13138 photos,
  corrects 82, and reports 11 as unchecked. A mismatch that is neither a swap nor a turn is still
  surfaced to the reader rather than silently mis-drawn.
- Dropping the `orientation` swap also removed the last disagreement between Inspect's geometry and
  `crossref`'s: `photo_syno_match` and `_face_targets` normalize by `photos.width`/`height`, and now
  so does Inspect's seed. The tier `assign_face` picks is therefore right on exactly the photos
  clustering is right on, which was not true when Inspect swapped and they did not.
- `resolve_frame` is O(candidates x turns x faces x syno_faces) per report — eight passes over a
  handful of boxes, on one photo, in a worker thread. It is not a per-request map over the library
  and needs no cache.
- Review cards no longer open Synology Photos in one click; it is two, via Inspect. That is the
  intended trade — the question a reviewer has in front of a wrong-looking card is nearly always
  about our own detection, not about Synology's copy of the photo.
