# Glossary

Glossary of terms for Synopticon for engineers who haven't touched ML/face detect before. Terms are grouped by where they show up in the pipeline. Within a group, foundational terms come first.

---

## Core ML concepts

**Model** — a file of learned numbers (see *weights*) plus a fixed recipe for the
math that turns an input into an output. Synopticon never trains models; it
downloads pre-trained ones and runs them. Weights are pinned by checksum in
`models/manifest.json` and never committed to the repo.

**Weights** — the actual learned parameters inside a model (millions of numbers).
"Loading the weights" = reading that file into memory. Different weights = a
different-behaving model even if the surrounding code is identical.

**Training vs. inference** — *training* is the (expensive, one-time) process of
learning the weights from example data; Synopticon does **none** of it. *Inference*
is *using* an already-trained model: feeding it an input and reading its output.
Everything Synopticon does with a model is inference.

**Inference** — running a trained model forward on an input to get its output. In
Synopticon this is (1) running a detector on a photo to find faces, and (2) running
an embedder on a face crop to get its embedding. It happens during the `extract`
phase (and `benchmark`); clustering/review/apply do no inference.

**Embedding** — the output of a face-recognition model: a fixed-length list of
numbers (a vector, e.g. 512 of them) that represents a face's identity. The key
property: two photos of the *same* person produce embeddings that are numerically
*close*; different people produce *distant* ones. Almost everything downstream
(clustering, merges, assignments) is really just arithmetic on embeddings.

**Embedder** — a model that produces embeddings (ArcFace, AdaFace, MagFace here),
as opposed to a *detector* that finds faces.

**Batch / batch size** — how many inputs are pushed through a model in one call.
Bigger batches use hardware (especially GPUs) more efficiently but need more memory.
`inference.batch_size` controls this for the embedding stage.

**Preprocessing / normalization** — the arithmetic done to pixel values *before*
they enter a model (e.g. scaling to a -1..1 range, or swapping colour channel order
to BGR). Each model was trained expecting a specific recipe; using the wrong one
silently produces garbage. This is why the code deliberately preprocesses each
embedder differently.

**L2-normalization** — rescaling a vector so its length is exactly 1, keeping only
its *direction*. Done to embeddings so that comparisons measure identity, not
loudness/magnitude. (See *cosine similarity*.)

**Hallucination** — when a generative model invents plausible-looking detail that
wasn't in the input. Relevant to face *restoration*: an over-aggressive restore can
"clean up" a blurry face into a subtly *different* person. Several restore settings
exist to detect and limit this.

---

## The face pipeline

**Detection / detector** — finding *where* faces are in a photo (drawing a box
around each), without yet knowing *who* they are. Synopticon runs two detectors
(SCRFD + YOLO) and fuses their results.

**Bounding box (bbox)** — the rectangle a detector draws around a face. Its size in
pixels is used as a rough quality/size filter (`min_face_px`, `trigger_px`).

**Landmarks** — a handful of labelled key points on a face (eyes, nose, mouth
corners) that a detector outputs alongside the box. Used to *align* the face.

**Alignment / aligned crop** — rotating and scaling a detected face (using its
landmarks) into a standard head-on, fixed-size image, so the embedder always sees
faces in a consistent pose. Embedders are run on aligned crops, not raw photos.

**Restoration** — optionally running a generative model (CodeFormer) to sharpen a
low-quality face crop before embedding. Optional, off by default, and advisory only
— restored embeddings never feed clustering (see *hallucination*).

**Quality signal** — a per-face number estimating how "good" the crop is. Here it's
derived from the *magnitude* of MagFace's embedding (before L2-normalization);
low-quality faces are candidates for restoration or exclusion.

---

## The models

**ONNX / ONNX Runtime** — a portable file format for trained models (`.onnx`) and
the engine that runs them. It lets the same model run on CPU or GPU without
model-specific code.

**Execution provider** — ONNX Runtime's term for the backend that does the math:
`CPUExecutionProvider` or `CUDAExecutionProvider` (GPU). The `inference.device`
setting picks which.

**CUDA** — NVIDIA's GPU compute platform. "Running on CUDA" = running on an NVIDIA
GPU. Requires a GPU-enabled build; otherwise it falls back to CPU.

**SCRFD, YOLO (YOLOv8-face)** — the two face *detectors*. SCRFD is the primary one
(it also produces landmarks); YOLO is a second-opinion detector whose extra finds
are added in to catch faces SCRFD missed.

**ArcFace, AdaFace, MagFace** — the three face *embedders*. Each was trained
differently and has its own strengths; Synopticon runs all three and combines them
(see *ensemble*).

**CodeFormer** — the generative model used for optional face *restoration*.

---

## Similarity & clustering

**Cosine similarity** — the standard way to compare two embeddings: a number from
-1 to 1 measuring how aligned their *directions* are (1 = identical direction,
0 = unrelated). Higher = more likely the same person. Most thresholds in the
crossref/clustering settings are cosine cutoffs.

**Ensemble / fusion** — combining several models' outputs into one decision rather
than trusting a single model. Synopticon *fuses* the three embedders' similarities
into one score (`fusion_weights` sets each model's relative say).

**Centroid** — the average of a group of embeddings — a single vector standing in
for a whole cluster. Comparing two clusters' centroids is a cheap way to ask "are
these the same person?"

**Clustering** — grouping the faces into sets that are probably the same person,
*without* being told the identities in advance. This is the core unsupervised step:
it turns a pile of embeddings into candidate people.

**kNN graph (k-nearest-neighbours)** — a structure where each face is linked to its
*k* most-similar other faces. It's the scaffold the clustering algorithms run on.
`knn_k` sets how many neighbours per face.

**Edge** — a link between two faces in the graph, kept only if their similarity
clears `edge_threshold`. Clusters emerge from chains of surviving edges.

**Chinese Whispers** — the default clustering algorithm: it repeatedly lets each
face adopt the most common label among its graph neighbours until things settle.
Fast, and every face ends up with a label.

**HDBSCAN** — an alternative, density-based clustering algorithm that can leave
sparse/ambiguous faces *unlabelled* (as noise) rather than forcing them into a
group. Higher purity, more leftovers.

**Singleton** — a face (or cluster) of size one — nobody similar enough to group
with. Often the result of a threshold set too high.

**Noise** — HDBSCAN's label for faces it declines to cluster at all (too sparse to
be confident). Not an error — a deliberate "don't know."

**Label propagation** — the general technique Chinese Whispers uses: labels spread
across graph edges from neighbour to neighbour over several passes (*iterations*).

---

## Detection tuning

**Confidence / score / threshold** — a detector outputs a confidence (0–1) for each
face it thinks it sees; the *score threshold* (`scrfd_score`, `yolo_score`) is the
cutoff below which a detection is discarded. Lower = more faces but more false
alarms.

**IoU (Intersection over Union)** — a 0–1 measure of how much two boxes overlap
(overlap area ÷ combined area). Used to decide whether two boxes are "the same
face."

**NMS (Non-Max Suppression)** — cleanup that removes duplicate overlapping boxes for
the same face, keeping the highest-confidence one. `nms_iou` sets how much overlap
counts as a duplicate.

**Image pyramid / scales** — running the detector on the photo at several sizes
(e.g. original and 2× upscaled) so both large and small/distant faces get found.
Each extra scale is another full detector pass (more compute). `detection.scales`
controls it.

---

## Evaluation

**Precision** — of the faces the pipeline grouped as person X, what fraction really
*are* X. High precision = few wrong faces mixed in.

**Recall** — of all the faces that really *are* X, what fraction the pipeline
actually grouped as X. High recall = few of X's faces missed. Precision and recall
trade off against each other; most tuning is choosing where on that curve to sit.

**False positive** — something flagged that shouldn't be (a non-face detected, or a
wrong face assigned to a person).

**False merge** — wrongly concluding two different people are the same person. The
most damaging clustering error here, since a merge can be irreversible on the NAS.

**B-cubed (bcubed_precision / recall / f1)** — a specific precision/recall scoring
method for clustering, reported by `synopticon eval`. **F1** is their harmonic mean
— a single combined score.

**Grid search / sweep** — trying many combinations of setting values and comparing
the resulting scores, to find good ones. `eval grid-search` automates this.

**Holdout** — a set of known-correct labels held aside purely for scoring, so you're
measuring against ground truth rather than the pipeline grading its own homework.

---

## Synopticon-specific terms

**Crossref (cross-reference)** — the step that matches Synopticon's own clusters
against Synology's *existing* person labels, to decide what to suggest: assign a
face to a known person, flag a low-confidence guess, propose a brand-new person, or
propose merging people Synology split. Governed by the `[crossref]` settings.

**Variant (`orig` vs restored)** — each face can have embeddings from the original
crop and from a restored crop. Clustering only ever uses `orig`; restored embeddings
are advisory (used to *detect* restoration problems, not to group faces).

**Pipeline version** — a checksum of the model set + detection settings. If it
changes, previously-processed photos are re-run; if not, they're skipped. It's how
`extract` knows what still needs doing.
