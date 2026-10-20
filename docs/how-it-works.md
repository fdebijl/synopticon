# How it works

## The face-recognition pipeline

The pipeline runs in a fixed order, with each stage consuming the previous stage's output:

```
sync  →  extract  →  cluster  →  report  →  review  →  apply
(NAS→db) (detect     (group      (static    (approve/  (write
          faces)      faces)     HTML)      reject UI) back to NAS)
└──────── read-only ────────────────────────┘          └─ gated write ─┘
```

(`recluster` and `eval` are side-loops off `cluster`. They group the faces again from the cached embeddings with different parameters and never touch the NAS - useful for benchmarking and testing the best parameters for your system)

1. **`sync`** — pulls your photo list, people, and existing face labels from the NAS (read-only). Existing Synology labels are the base for Synopticon's face grouping.
2. **`extract`** (*Detect faces*) — downloads originals (the originals are not kept on disk to save on space, only small face crops are kept), detects faces with two detectors at multiple scales, and computes up to three embeddings per face into the database. Resumable per photo, so you can run it over multiple hours or days to do the initial catchup run.
3. **`cluster`** (*Group faces*) — builds an exact k-NN similarity graph over all faces, groups them (Chinese Whispers by default), and maps each group onto a Synology person by majority vote. Produces proposals for new face assignments, wrong-label corrections (a face Synology tagged as one person that clearly belongs to another), merge candidates, and new people.
4. **`report` / `review`** — a static HTML report and an interactive web UI to approve or reject each proposal. Nothing is written until you approve it.
5. **`apply`** — pushes approved assignments back via the `add_face` API (works even on photos Synology found no face in). Dry-run by default; merges require a second explicit flag; every write is audit-logged and assignments are individually reversible.
6. **`recluster` / `eval`** — retune thresholds from the embedding cache (no re-detection, no NAS traffic) and measure quality by masking known labels and checking recovery.

## Deduplication

`dedupe` is independent of the face pipeline. Run `sync --hash` at least once to store a sha256 and a 64-bit perceptual hash for every original, then `dedupe` groups byte-identical (`--exact`) and/or visually near-identical (`--visual`) photos. It keeps the best copy of each group (highest resolution, then largest file), and deletes the rest through Synology's background-task API. Like `apply`, it is **dry-run by default**, prints a Synology Photos deep link for every photo it would touch, and audit-logs every deletion.
