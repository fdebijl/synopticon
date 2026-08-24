"""Layered configuration: defaults -> TOML file -> SYNOPTICON_* env vars (env wins).

The config file path is taken from SYNOPTICON_CONFIG, defaulting to
./config.toml then /data/config.toml. Credentials are recommended env-only:
SYNOPTICON_NAS__ACCOUNT / SYNOPTICON_NAS__PASSWORD.

Field help text is rendered verbatim in the web Settings UI and reads top-down
from plain language into detail: `description` opens with what the setting does
in ordinary terms and then gives practical guidance, while the internals go in
`json_schema_extra={"details": ...}`, which the GUI renders as a collapsed,
de-emphasized block under the description.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

Space = Literal["personal", "shared"]


class NasConfig(BaseModel):
    url: str = ""
    # Base URL for Synology Photos web-UI deep links in the review UI. The API
    # host/port (`url`) is often not where the web UI lives, so allow an
    # override; falls back to `url` when unset.
    web_url: str = ""
    verify_tls: bool = True
    account: str = ""
    password: SecretStr = SecretStr("")
    otp_code: str | None = None
    device_name: str = "synopticon"
    spaces: list[Space] = Field(default_factory=lambda: ["personal"])
    requests_per_second: float = 4.0
    write_requests_per_second: float = 1.0
    timeout_s: float = 60.0


class StorageConfig(BaseModel):
    # Repo-root-relative defaults for bare-metal runs; the Docker image
    # overrides both via env to its /data and /models volume mounts.
    data_dir: Path = Path("./data")
    models_dir: Path = Path("./models")
    keep_originals: bool = False
    originals_cache_gb: float = 50.0

    @property
    def db_path(self) -> Path:
        return self.data_dir / "synopticon.db"

    @property
    def originals_dir(self) -> Path:
        return self.data_dir / "originals"

    @property
    def crops_dir(self) -> Path:
        return self.data_dir / "crops"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "report"


class DatabaseConfig(BaseModel):
    backend: Literal["sqlite", "postgres"] = Field(
        default="sqlite",
        title="Backend",
        description=(
            "Where Synopticon keeps its own database — the photo index, the faces "
            "it found, and your review decisions. This is never your NAS.\n\n"
            "'sqlite' (default) is a single file under the data directory. It needs "
            "no setup, no server and no maintenance, and it is the right choice "
            "unless you specifically want otherwise.\n"
            "'postgres' points Synopticon at an existing PostgreSQL server instead, "
            "using the connection settings below. Useful if you already run one, "
            "want the database on different storage from the photo cache, or want "
            "to back it up with the tools you already have.\n\n"
            "Switching backends does not move your data: use `synopticon db-migrate` "
            "to copy an existing library across."
        ),
        json_schema_extra={
            "details": (
                "PostgreSQL needs the optional 'postgres' extra "
                "(uv sync --extra postgres), which installs psycopg 3 and its "
                "connection pool.\n\n"
                "Schema and SQL are shared: db/schema.sql and every migration are "
                "authored in SQLite's dialect and translated per backend at "
                "migration time (db/dialect.py). Schema versioning uses "
                "PRAGMA user_version on SQLite and a synopticon_schema_version "
                "table on PostgreSQL, with a session advisory lock so a web server "
                "and a job subprocess starting together cannot both migrate.\n\n"
                "MySQL/MariaDB are not supported: their upsert syntax differs from "
                "the ON CONFLICT form every sync path uses, and TEXT columns cannot "
                "be primary keys there."
            )
        },
    )
    host: str = Field(
        default="localhost",
        title="Host",
        description="Hostname or IP address of the PostgreSQL server.",
    )
    port: int = Field(
        default=5432,
        title="Port",
        description="Port the PostgreSQL server listens on. The default is 5432.",
    )
    user: str = Field(
        default="synopticon",
        title="User",
        description="PostgreSQL role to connect as. It needs to own the database.",
    )
    password: SecretStr = Field(
        default=SecretStr(""),
        title="Password",
        description="Password for that role. Leave empty if the server trusts the connection.",
    )
    database: str = Field(
        default="synopticon",
        title="Database",
        description=(
            "Name of the database to use. It has to exist already — Synopticon "
            "creates its tables inside it, but it will not create the database."
        ),
    )
    sslmode: str = Field(
        default="prefer",
        title="SSL mode",
        description=(
            "How hard to insist on an encrypted connection. 'prefer' (default) "
            "encrypts when the server offers it, 'require' refuses to connect "
            "otherwise, 'disable' never encrypts."
        ),
        json_schema_extra={
            "details": (
                "Passed straight through to libpq as the sslmode parameter; the "
                "full set is disable, allow, prefer, require, verify-ca, verify-full."
            )
        },
    )
    pool_size: int = Field(
        default=5,
        title="Connection pool size",
        description=(
            "How many PostgreSQL connections Synopticon keeps open at once. The "
            "default suits a single web server; raise it only if the interface "
            "feels queued under heavy use."
        ),
        json_schema_extra={
            "details": (
                "The web app opens a connection per request, so without pooling "
                "every dashboard poll would cost a TCP round trip plus "
                "authentication. One pool per DSN per process; job subprocesses "
                "build their own."
            )
        },
    )
    url: SecretStr = Field(
        default=SecretStr(""),
        title="Connection URL",
        description=(
            "Optional. A complete postgresql:// connection string, which overrides "
            "every field above. Managed providers usually hand you one of these "
            "ready to paste."
        ),
        json_schema_extra={
            "details": (
                "Passed to libpq verbatim, so any parameter it accepts works here "
                "(sslmode, connect_timeout, options, …). Stored as a secret because "
                "it normally embeds the password."
            )
        },
    )


class InferenceConfig(BaseModel):
    device: Literal["auto", "cpu", "cuda"] = Field(
        default="auto",
        title="Device",
        description=(
            "Whether the face models run on your CPU or on an NVIDIA GPU. A GPU is much "
            "faster; the CPU works everywhere.\n\n"
            "'auto' (default) uses the GPU when a working one is present and quietly "
            "falls back to the CPU otherwise.\n"
            "'cuda' asks for the GPU explicitly, and still falls back to the CPU with a "
            "warning if it is unavailable.\n"
            "'cpu' never touches the GPU and is the portable choice."
        ),
        json_schema_extra={
            "details": (
                "Selects the ONNX Runtime execution provider for every detection and "
                "embedding model. Confirm the effective device in the extract startup log "
                "('running on GPU/CPU') or via `synopticon hwinfo` — if you set 'cuda' but "
                "see CPU, CUDA init failed, usually because a CPU-only onnxruntime wheel "
                "is installed."
            )
        },
    )
    device_id: int = Field(
        default=0,
        title="CUDA Device Ordinal",
        description=(
            "Which GPU to use on a machine that has more than one. 0 is the first GPU; "
            "leave it alone on single-GPU or CPU-only machines.\n\n"
            "The numbering matches nvidia-smi, and the setting is ignored on CPU. There is "
            "no built-in multi-GPU sharding — one run uses one GPU. For the twelve people "
            "that still run SLI, run several instances of Synopticon with this value "
            "incremented to parallelize."
        ),
        json_schema_extra={
            "details": (
                "The CUDA device ordinal the session is pinned to. An out-of-range ordinal "
                "makes CUDA init fail and silently fall back to CPU, so verify the "
                "intended GPU shows load in nvidia-smi during an extract/benchmark run."
            )
        },
    )
    batch_size: int = Field(
        default=16,
        title="Embedding Batch Size",
        description=(
            "How many face crops are handed to each recognition model at once. Larger "
            "batches use a GPU more efficiently but need more memory.\n\n"
            "Typical 8-64, default 16: raise to 32-64 on a GPU with headroom, keep at 8-16 "
            "on CPU or a small GPU, and lower it if you hit out-of-memory errors during "
            "the embed stage."
        ),
        json_schema_extra={
            "details": (
                "Applies to the embedding stage only — detectors run one image at a time. "
                "The effective batch per photo is capped at the number of faces found in "
                "that photo, so raising it only helps crowded group shots. Measure the "
                "'embed' stage with `synopticon benchmark`."
            )
        },
    )
    intra_op_threads: int | None = Field(
        default=None,
        title="ONNX Runtime Intra-Op Threads",
        description=(
            "How many CPU threads the face models may use. This is the main speed knob "
            "when running on CPU; leave it blank to use every physical core.\n\n"
            "Set an explicit lower value (2-4) to leave headroom for other work, or when "
            "running several extract processes at once so they don't fight over cores. "
            "Going above the physical core count rarely helps."
        ),
        json_schema_extra={
            "details": (
                "ONNX Runtime's intra-op thread count (parallelism within a single "
                "operator). The blank default is the physical core count — hyperthreads "
                "are deliberately excluded, since they rarely help compute-bound ops — and "
                "an explicit value also avoids oversubscription against BLAS/OMP pools. "
                "Tune by comparing `synopticon benchmark` stage times across values."
            )
        },
    )
    job_threads: int | None = Field(
        default=None,
        title="Job BLAS/OpenMP Threads",
        description=(
            "How many CPU threads a job started from the web GUI may use. Blank (default) "
            "means one less than the core count, which keeps a core free so the web "
            "interface stays responsive.\n\n"
            "This is the knob to turn if the GUI crawls while a job runs. Set 0 to leave "
            "the environment alone entirely; a value the operator has already exported "
            "always wins."
        ),
        json_schema_extra={
            "details": (
                "Exported to the job subprocess as OMP_NUM_THREADS and the "
                "BLAS/OpenMP/numexpr aliases. Clustering multiplies large matrices through "
                "BLAS, which by default spawns one busy-spinning thread per core and "
                "starves the single uvicorn process — the symptom is unrelated requests "
                "all taking tens of seconds and completing at the same instant. Does not "
                "govern ONNX Runtime, which has its own pool: see intra_op_threads."
            )
        },
    )
    job_nice: int = Field(
        default=10,
        ge=0,
        le=19,
        title="Job Niceness",
        description=(
            "How politely jobs started from the web GUI compete for CPU with everything "
            "else. Higher means the job yields more.\n\n"
            "0-19; the default 10 lets an interactive request win against a batch job "
            "without measurably slowing the job down, since priority only matters while "
            "the two compete. Set 0 to run jobs at the same priority as the server."
        ),
        json_schema_extra={
            "details": (
                "The scheduling niceness applied to job subprocesses after spawn."
            )
        },
    )


class DetectionConfig(BaseModel):
    scales: list[float] = Field(
        default_factory=lambda: [1.0, 2.0],
        title="SCRFD Scales",
        description=(
            "How many times each photo is re-examined at a larger size, so small or "
            "distant faces are still found.\n\n"
            "[1.0] is a single fast pass and is weak on small faces; [1.0, 2.0] (default) "
            "adds a doubled-size pass that recovers them. Add a bigger factor if small "
            "faces are frequently missed, or drop the 2.0 if runtime or memory blows up on "
            "large images."
        ),
        json_schema_extra={
            "details": (
                "Image-pyramid factors for the SCRFD detector only (YOLO is unaffected) — "
                "the full frame is resized by each factor and re-inferenced. Each extra "
                "scale is a full forward pass, so a factor of 2.0 costs roughly 4x the "
                "pixels, compute and memory; large factors are clamped by max_long_side."
            )
        },
    )
    scrfd_score: float = Field(
        default=0.45,
        title="SCRFD Score",
        description=(
            "How sure the main face detector must be before it accepts something as a "
            "face. Lower finds more faces, but also more things that aren't faces.\n\n"
            "0-1, typical 0.2-0.5; the default 0.45 favours precision. Lower it to "
            "recover profile, occluded or blurry faces at the cost of false positives "
            "feeding clustering; raise it if textured backgrounds are being detected as "
            "faces."
        ),
        json_schema_extra={
            "details": (
                "The confidence threshold for SCRFD, the primary detector whose boxes are "
                "the ones that keep facial landmarks. Re-run the extract stage to evaluate "
                "a change."
            )
        },
    )
    yolo_score: float = Field(
        default=0.45,
        title="YOLO Score",
        description=(
            "How sure the secondary face detector must be. It exists to catch faces the "
            "main detector missed.\n\n"
            "0-1, typical 0.25-0.5; default 0.45, matching scrfd_score so its extra "
            "detections stay as trustworthy as the primary ones. Lower it to have it "
            "contribute more novel faces (with more false positives); raise it if it adds "
            "spurious boxes. No effect if the YOLO model is absent."
        ),
        json_schema_extra={
            "details": (
                "The confidence threshold for the recall-oriented YOLOv8-face detector, "
                "whose unmatched boxes become additional faces. Evaluate it on that delta "
                "— the faces present only because YOLO fired. Re-run the extract stage to "
                "evaluate a change."
            )
        },
    )
    nms_iou: float = Field(
        default=0.45,
        title="Intra-Detector NMS IoU",
        description=(
            "How much two boxes from the same detector must overlap before they are "
            "treated as one face. This cleans up duplicate boxes stacked on a single "
            "face.\n\n"
            "Typical 0.3-0.6; default 0.45. Lower it if one face ends up with several "
            "boxes (common when using multiple scales); raise it if two adjacent faces "
            "(cheek-to-cheek) collapse into one."
        ),
        json_schema_extra={
            "details": (
                "The Intersection-over-Union threshold for per-detector Non-Max "
                "Suppression, which de-duplicates each detector's own multi-scale boxes. "
                "This is not the SCRFD ↔ YOLO fusion threshold — see cross_iou."
            )
        },
    )
    cross_iou: float = Field(
        default=0.50,
        title="SCRFD ↔ YOLO Fusion IoU",
        description=(
            "How much a box from each of the two detectors must overlap before they count "
            "as one face rather than two.\n\n"
            "Typical 0.3-0.6; default 0.50. Lower it if the same face appears twice after "
            "fusion, once per detector; raise it if two nearby distinct faces get merged "
            "into one. No effect without the YOLO model."
        ),
        json_schema_extra={
            "details": (
                "The Intersection-over-Union threshold for fusing SCRFD and YOLO "
                "detections. Above it the YOLO box is treated as the same face and the "
                "SCRFD box with its landmarks is kept; below it the YOLO box is added as a "
                "new face."
            )
        },
    )
    min_face_px: int = Field(
        default=20,
        title="Minimum Face Size (px)",
        description=(
            "The smallest face worth keeping, in pixels. Smaller detections are thrown "
            "away.\n\n"
            "Typical 16-40; default 20. Raise it if clustering is being degraded by "
            "garbage faces; lower it only if you genuinely need distant background faces."
        ),
        json_schema_extra={
            "details": (
                "Measured on the shorter box side, in original-image pixels. Tiny crops "
                "produce weak, noisy embeddings that pollute clustering. Tune together "
                "with scales, which recovers small faces this filter may then discard."
            )
        },
    )
    max_long_side: int = Field(
        default=6000,
        title="Max Long Side (px)",
        description=(
            "A ceiling on how large an image may be blown up while looking for faces, so "
            "big photos don't exhaust memory.\n\n"
            "Typical 4000-8000, memory-bound; default 6000. Lower it if you hit "
            "out-of-memory errors or high-megapixel images are slow; raise it if your "
            "scales include 2.0 or more but small faces on large images still aren't "
            "recovered — the clamp is probably cancelling the upscale."
        ),
        json_schema_extra={
            "details": (
                "Caps SCRFD's upscaled long side in pixels, bounding the memory cost of "
                "the scales pyramid; an upscale factor is reduced so long_side × factor "
                "stays under it. It caps upscaling only and never shrinks an already-large "
                "original. SCRFD only."
            )
        },
    )
    yolo_upscale_below_px: int = Field(
        default=1600,
        title="YOLO Upscale Threshold (px)",
        description=(
            "Below this image size, the secondary detector takes a second look at double "
            "resolution, so small faces in small photos aren't missed.\n\n"
            "Typical 1000-2500; default 1600. Raise it if small faces in medium-resolution "
            "(~640-1600px) images are missed; lower it if YOLO latency dominates and "
            "small-face recall isn't needed."
        ),
        json_schema_extra={
            "details": (
                "A long-side threshold in pixels. YOLO always letterboxes its input to "
                "640px, so small images otherwise waste resolution. YOLO only; independent "
                "of SCRFD's scales."
            )
        },
    )


class RestorationConfig(BaseModel):
    enabled: bool = Field(
        default=False,
        title="Enable Restoration",
        description=(
            "Turns on an optional pass that tries to clean up small or poor-quality face "
            "crops before they are recognized.\n\n"
            "Off by default, and free when off. Restoration never feeds clustering: it "
            "only fills a second 'restored' embedding and flags faces where the cleanup "
            "changed the identity too much, as a quality-assurance aid. Leave it off "
            "unless you have set it up for exactly that."
        ),
        json_schema_extra={
            "details": (
                "Requires the [restore] extra — which pins old torch/torchvision, is "
                "currently incompatible with the GPU extra (CPU inference only) — plus a "
                "vendored CodeFormer model; without those it fails at startup or on the "
                "first crop. Fills the 'restored' embedding variant and files "
                "restore_disagreement review flags."
            )
        },
    )
    trigger_px: int = Field(
        default=80,
        title="Restoration Trigger Size (px)",
        description=(
            "Faces smaller than this are always restored, however good they look.\n\n"
            "Default 80; practical 40-160. Raise it to also restore mid-size faces (more "
            "compute, and more risk of hallucinating on faces that were fine); lower it to "
            "restore only genuinely tiny crops."
        ),
        json_schema_extra={
            "details": (
                "Measured on the shorter bounding-box side and OR'd with the quality gate "
                "below, so the effective restoration band is roughly "
                "[detection.min_face_px, trigger_px). Watch which crops actually get "
                "restored via faces.restored=1 against bbox size."
            )
        },
    )
    quality_percentile: float = Field(
        default=15.0,
        title="Restoration Quality Percentile",
        description=(
            "Also restore the worst-looking share of each batch of faces — this is that "
            "percentage.\n\n"
            "Default 15.0, range 0-100. Raise for more coverage (more compute, more "
            "hallucination risk); lower to target only the worst faces; 0 effectively "
            "disables this gate and leaves only the size trigger."
        ),
        json_schema_extra={
            "details": (
                "Quality is the MagFace embedding norm, and the cut is relative per batch "
                "rather than absolute — a fixed fraction is always eligible even in an "
                "all-good batch. Evaluate on representative batches, since composition "
                "matters."
            )
        },
    )
    fidelity: float = Field(
        default=0.7,
        title="Restoration Fidelity Weight",
        description=(
            "How closely restoration must stick to the original face. Higher stays "
            "faithful but enhances less; lower looks better but risks quietly turning the "
            "face into someone else.\n\n"
            "0-1; the default 0.7 leans toward fidelity, which is what a recognition "
            "pipeline wants. This is the primary knob against over-restoration — if "
            "lowering it pushes many faces over disagreement_cos, the model is inventing "
            "identity, so raise it back."
        ),
        json_schema_extra={
            "details": (
                "CodeFormer's fidelity weight w. Safe to A/B, since restored embeddings "
                "never feed clustering."
            )
        },
    )
    disagreement_cos: float = Field(
        default=0.30,
        title="Restoration Disagreement Cosine",
        description=(
            "How different a restored face may look from the original before a human is "
            "asked to check it.\n\n"
            "Default 0.30; practical band ~0.1-0.6. Lower it to catch subtle identity "
            "drift early, at the cost of a larger review queue; raise it to flag only "
            "gross changes."
        ),
        json_schema_extra={
            "details": (
                "Measured as 1 − cosine between the restored and original embeddings, so "
                "0.30 ≈ cosine 0.70 (theoretical max 2.0). This is the direct 'did "
                "restoration hallucinate a different identity' detector — tune it together "
                "with fidelity and watch the restore_disagreement queue volume."
            )
        },
    )


class ClusteringConfig(BaseModel):
    knn_k: int = Field(
        default=64,
        title="kNN Neighbor Count",
        description=(
            "How many look-alike candidates each face is compared against when faces are "
            "grouped into people.\n\n"
            "Typical 20-128; default 64. Too low and someone who appears in hundreds of "
            "photos gets fragmented in a way no threshold can fix; too high adds "
            "wrong-person candidates and costs quadratic compute. Raise it until recall "
            "(bcubed_recall in `eval`) plateaus."
        ),
        json_schema_extra={
            "details": (
                "The number of nearest neighbors retrieved per face when building the "
                "cosine-similarity kNN graph — an upper bound on connectivity, not a "
                "cluster size. Changing it (or fusion_weights) invalidates the graph cache "
                "and forces a full recompute, unlike edge_threshold."
            )
        },
    )
    edge_threshold: float = Field(
        default=0.50,
        title="Edge Threshold",
        description=(
            "How similar two faces must look to be treated as the same person. This is the "
            "single most important face-grouping setting.\n\n"
            "0-1, sweet spot ~0.40-0.60; default 0.50. Too low lets weak links chain "
            "distinct people into giant merged groups; too high fragments one identity "
            "over pose, age and lighting variation. Faces left with no surviving link "
            "become one-face groups."
        ),
        json_schema_extra={
            "details": (
                "The cosine-similarity cutoff for keeping an edge in the graph Chinese "
                "Whispers runs on; HDBSCAN ignores it. It is the cheapest knob to sweep, "
                "since it does not invalidate the graph cache — use `recluster --set` or "
                "`eval grid-search` and watch the bcubed precision/recall tradeoff."
            )
        },
    )
    algorithm: Literal["chinese_whispers", "hdbscan"] = Field(
        default="chinese_whispers",
        title="Grouping Algorithm",
        description=(
            "Which method is used to group faces into people.\n\n"
            "'chinese_whispers' (default) gives every face a group and is tuned with "
            "edge_threshold — fast, with no extra dependency. 'hdbscan' instead rejects "
            "faces in sparse regions as noise: higher precision, lower recall, more "
            "explicit one-offs, and tuned with min_cluster_size."
        ),
        json_schema_extra={
            "details": (
                "chinese_whispers is iterative label propagation on the thresholded graph. "
                "hdbscan is density clustering, needs the optional hdbscan dependency, and "
                "ignores edge_threshold and cw_iterations. Compare the two on the same "
                "holdout with `eval grid-search` — the graph cache is reused, so the A/B "
                "is cheap."
            )
        },
    )
    cw_iterations: int = Field(
        default=30,
        title="Chinese Whispers Iterations",
        description=(
            "How many passes the default grouping method makes over the faces before it "
            "stops.\n\n"
            "Typical 10-50; the default 30 is deliberately over-provisioned. Too few "
            "leaves identities under-merged; more than needed only wastes time and cannot "
            "cause over-merging. HDBSCAN ignores it."
        ),
        json_schema_extra={
            "details": (
                "Label-propagation sweeps, with no early stopping — it always runs exactly "
                "this many. Graph topology is fixed by edge_threshold, not by this. Tune "
                "with `recluster --set clustering.cw_iterations=N`: raise it if "
                "assignments still shift between 30 and 40, lower it if they are identical "
                "from 15 up."
            )
        },
    )
    min_cluster_size: int = Field(
        default=2,
        title="HDBSCAN Minimum Group Size",
        description=(
            "The fewest faces HDBSCAN needs before it will call them a group. Has no "
            "effect under the default chinese_whispers algorithm.\n\n"
            "Default 2 (the minimum) maximizes recall. Raise it to 3-5 for purer groups, "
            "at the cost of losing real people who only appear in two or three photos. For "
            "the minimum size to propose a brand-new person, see "
            "crossref.new_person_min_faces instead."
        ),
        json_schema_extra={
            "details": (
                "Connected components smaller than this are pruned to noise, and the value "
                "is also used as min_samples, so larger values label more faces as noise. "
                "Integers below 2 are clamped up."
            )
        },
    )
    seed: int = Field(
        default=42,
        title="Chinese Whispers RNG Seed",
        description=(
            "A fixed number that makes repeated runs come out identical. Not a quality "
            "knob — leave it as it is.\n\n"
            "Keeping it fixed (default 42) makes review queues reproducible and grid "
            "searches comparable. Only vary it to probe robustness: large swings in group "
            "assignments across seeds mean the graph is over-connected, so raise "
            "edge_threshold rather than hunting for a 'good' seed."
        ),
        json_schema_extra={
            "details": (
                "Seeds Chinese Whispers' per-iteration node visit order; HDBSCAN does not "
                "use it. High cross-seed variance indicates a graph near a percolation "
                "point."
            )
        },
    )
    fusion_weights: dict[str, float] = Field(
        default_factory=dict,
        title="Per-Model Fusion Weights",
        description=(
            "How much each recognition model counts when deciding whether two faces match. "
            "Empty (default) weights them all equally.\n\n"
            "Keyed by embedding model name (arcface, adaface, magface); a model that isn't "
            "listed counts as 1.0, and only the ratios matter. Push a weight toward 0 to "
            "suppress a model that is noisy on your data — but over-weighting one model "
            "discards the benefit of using an ensemble at all."
        ),
        json_schema_extra={
            "details": (
                "Each model block is L2-normalized before fusing, so a model's share of "
                "the final similarity scales with weight². Changing any weight invalidates "
                "the graph cache. Tune with `recluster --set` plus `eval holdout` "
                "(bcubed_f1), and make sure the names match the embeddings table exactly."
            )
        },
    )


class CrossrefConfig(BaseModel):
    majority: float = Field(
        default=0.60,
        title="Majority Fraction",
        description=(
            "How much of a face group must already carry the same Synology name before "
            "Synopticon accepts that the group is that person.\n\n"
            "~0.50-0.80; the default 0.60 tolerates one dissenting label at min_labeled=3. "
            "Raise it if wrong-person assigns appear; lower it if coherent, well-labeled "
            "groups produce no assigns at all. Too low also inflates false merges."
        ),
        json_schema_extra={
            "details": (
                "The fraction is over the cluster's *labeled* faces only, and is paired "
                "with min_labeled. This mapping gates every downstream suggestion — "
                "assign, reassign and cluster-pair merges."
            )
        },
    )
    min_labeled: int = Field(
        default=3,
        title="Minimum Labeled Faces",
        description=(
            "How many faces in a group must already be tagged in Synology before its "
            "majority name is trusted at all.\n\n"
            "Sensible 2-6; default 3. At 1, a single tagged face maps the whole group, "
            "which is fragile. Raise it if groups get mapped off one or two faces that "
            "turn out wrong; lower it toward 2 if your library is sparsely tagged and real "
            "groups rarely reach three labels."
        ),
        json_schema_extra={
            "details": (
                "Below this the cluster is ignored entirely — no assign, no merge. Also "
                "the leave-one-out support floor for reassigns."
            )
        },
    )
    assign_sim: float = Field(
        default=0.55,
        title="Assign Similarity Cutoff",
        description=(
            "How confident a match must be before it is proposed as a straightforward "
            "assignment rather than flagged as uncertain.\n\n"
            "Practical 0.40-0.70; default 0.55. Both outcomes are still reviewable — this "
            "only decides which queue a face lands in, and it is the confidence score "
            "shown in the review UI. Raise it if wrong faces land in 'assign'; lower it if "
            "many correct faces are stuck in 'low_confidence'."
        ),
        json_schema_extra={
            "details": (
                "An unlabeled face's mean cosine similarity to the cluster's labeled "
                "faces, range [-1,1]. Also the floor below which reassign claims are "
                "discarded as noise."
            )
        },
    )
    new_person_min_faces: int = Field(
        default=5,
        title="New Person Minimum Faces",
        description=(
            "How many faces a completely unknown person must have before Synopticon "
            "proposes creating them as a new person.\n\n"
            "Default 5; sensible 3-15. Raise it if the new_person queue fills with junk "
            "(blurry or partial faces, one-off strangers); lower it if genuine recurring "
            "people who appear in few photos are missed. Paired with "
            "new_person_min_photos, which counts distinct photos instead."
        ),
        json_schema_extra={
            "details": (
                "A size floor on fully-unlabeled clusters (zero Synology matches), not a "
                "similarity — it filters small, likely-spurious clusters. Quality here "
                "depends on upstream clustering coherence."
            )
        },
    )
    new_person_min_photos: int = Field(
        default=8,
        title="New Person Minimum Photos",
        description=(
            "How many different photos a completely unknown person must appear in before "
            "Synopticon proposes creating them as a new person.\n\n"
            "Default 8; sensible 2-15. A burst of near-identical shots can push a stranger "
            "past the face count while still being a single moment, so this asks for "
            "spread as well as size. Lower it if people who appear in only a handful of "
            "photos are being missed."
        ),
        json_schema_extra={
            "details": (
                "Counts distinct (space, photo_id) pairs among the cluster's faces, and "
                "applies on top of new_person_min_faces — a cluster must clear both. Set "
                "to 1 to disable."
            )
        },
    )
    merge_vote_fraction: float = Field(
        default=0.30,
        title="Merge Vote Fraction",
        description=(
            "When one face group carries two different Synology names, this is how much of "
            "the group each name needs before Synopticon suggests that Synology split one "
            "person in two.\n\n"
            "~0.20-0.50; default 0.30, deliberately below 0.5 so a group can nominate more "
            "than two co-dominant persons. Raise it for fewer, more confident merge "
            "suggestions; lower it to catch more Synology over-splits, at the cost of "
            "false merges from a few stray mislabeled faces."
        ),
        json_schema_extra={
            "details": (
                "The first merge trigger (intra-cluster). Watch the merge/merge_named "
                "queues for false merges."
            )
        },
    )
    merge_centroid_sim: float = Field(
        default=0.60,
        title="Merge Centroid Similarity",
        description=(
            "How alike two separate face groups must be on average before Synopticon "
            "suggests their two Synology people are really the same person.\n\n"
            "Practical 0.50-0.75; default 0.60, lower than face-to-face thresholds because "
            "averaged faces are less distinctive. Raise it so only near-identical groups "
            "merge; lower it if one person is clearly fragmented across groups that never "
            "get proposed."
        ),
        json_schema_extra={
            "details": (
                "The second merge trigger (inter-cluster) — clusters that mapped to "
                "*different* persons but whose mean-embedding centroids are closer than "
                "this cosine, range [-1,1]. Tune together with merge_vote_fraction and "
                "judge by the combined false-merge rate. Both merge kinds still require "
                "explicit apply-time gates before anything is written."
            )
        },
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SYNOPTICON_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    nas: NasConfig = Field(default_factory=NasConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    inference: InferenceConfig = Field(default_factory=InferenceConfig)
    detection: DetectionConfig = Field(default_factory=DetectionConfig)
    restoration: RestorationConfig = Field(default_factory=RestorationConfig)
    clustering: ClusteringConfig = Field(default_factory=ClusteringConfig)
    crossref: CrossrefConfig = Field(default_factory=CrossrefConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Earlier sources win: init > env > .env file > toml.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls, toml_file=_config_file()),
        )


def _config_file() -> Path | None:
    explicit = os.environ.get("SYNOPTICON_CONFIG")
    if explicit:
        return Path(explicit)
    for candidate in (
        Path("config.toml"),
        Path("data/config.toml"),  # the compose-mounted location, bare-metal view
        Path("/data/config.toml"),  # same location, in-container view
    ):
        if candidate.is_file():
            return candidate
    return None


def load_settings(**overrides) -> Settings:
    return Settings(**overrides)
