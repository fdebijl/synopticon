"""Layered configuration: defaults -> TOML file -> SYNOPTICON_* env vars (env wins).

The config file path is taken from SYNOPTICON_CONFIG, defaulting to
./config.toml then /data/config.toml. Credentials are recommended env-only:
SYNOPTICON_NAS__ACCOUNT / SYNOPTICON_NAS__PASSWORD.
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


class InferenceConfig(BaseModel):
    device: Literal["auto", "cpu", "cuda"] = Field(
        default="auto",
        description=(
            "ONNX Runtime execution provider for all detection/embedding models. "
            "'auto' uses the GPU when a working CUDAExecutionProvider is present and "
            "silently falls back to CPU otherwise 'cuda' forces GPU (still falls back "
            "to CPU with a warning if unavailable); 'cpu' is CPU-only and portable. "
            "Confirm the effective device in the extract startup log ('running on "
            "GPU/CPU') and via `synopticon diagnostics` — if you set 'cuda' but see CPU, "
            "CUDA fell back, usually because a CPU-only onnxruntime wheel is installed."
        ),
    )
    device_id: int = Field(
        default=0,
        description=(
            "CUDA device ordinal to pin the session to on multi-GPU hosts (0 = first "
            "GPU, matching nvidia-smi indices). Ignored on CPU. There is no built-in "
            "multi-GPU sharding — one run uses one GPU. An out-of-range ordinal makes "
            "CUDA init fail and silently fall back to CPU, so verify the intended GPU "
            "shows load in nvidia-smi during an extract/benchmark run."
        ),
    )
    batch_size: int = Field(
        default=16,
        description=(
            "Number of aligned face crops sent to each embedder per inference call "
            "(embedding stage only; detectors run one image at a time). The effective "
            "batch per photo is capped at the faces found in that photo, so raising it "
            "only helps crowded/group shots. Typical 8-64 (default 16): larger improves "
            "GPU utilization at higher peak memory — raise to 32-64 on a GPU with "
            "headroom, keep at 8-16 on CPU or small GPUs, and lower it if you hit CUDA "
            "OOM during embed. Measure the 'embed' stage with `synopticon benchmark`."
        ),
    )
    intra_op_threads: int | None = Field(
        default=None,
        description=(
            "ONNX Runtime intra-op thread count (parallelism within a single operator) "
            "— the main CPU-throughput knob. Leave blank to use the physical core count "
            "(hyperthreads are deliberately excluded, as they rarely help compute-bound "
            "ops). Set an explicit lower value (e.g. 2-4) to leave headroom or when "
            "running several extract processes at once, to avoid oversubscription with "
            "BLAS/OMP threads; going above the physical core count rarely helps. Tune by "
            "comparing `synopticon benchmark` stage times across values."
        ),
    )


class DetectionConfig(BaseModel):
    scales: list[float] = Field(
        default_factory=lambda: [1.0, 2.0],
        description=(
            "Image-pyramid factors for the SCRFD detector (does not affect YOLO): the "
            "full frame is resized by each factor and re-inferenced, so faces are seen "
            "at multiple resolutions. [1.0] is a single fast pass, weak on small faces; "
            "[1.0, 2.0] (default) adds a 2× upscale to recover small/distant faces. Each "
            "extra scale is a full forward pass — a 2.0 factor is ~4× the "
            "pixels/compute/memory — and large factors are clamped by max_long_side. Add "
            "a bigger factor if small faces are missed; drop 2.0 if runtime or memory "
            "blows up on large images."
        ),
    )
    scrfd_score: float = Field(
        default=0.30,
        description=(
            "Confidence threshold (0-1) for the primary SCRFD detector, whose boxes are "
            "the ones that keep facial landmarks. Typical 0.2-0.5; default 0.30 leans "
            "toward recall. Lower it to recover profile/occluded/blurry faces at the "
            "cost of more false positives feeding clustering; raise it if textured "
            "backgrounds are being detected as faces."
        ),
    )
    yolo_score: float = Field(
        default=0.35,
        description=(
            "Confidence threshold (0-1) for the secondary, recall-oriented YOLOv8-face "
            "detector, whose unmatched boxes add faces SCRFD missed. Typical 0.25-0.5; "
            "default 0.35, kept slightly above scrfd_score so its extra detections stay "
            "trustworthy. Lower it to have YOLO contribute more novel faces (more false "
            "positives); raise it if YOLO adds spurious boxes. Evaluate on the delta — "
            "faces present only because YOLO fired. No effect if the YOLO model is absent."
        ),
    )
    nms_iou: float = Field(
        default=0.45,
        description=(
            "IoU threshold for the per-detector Non-Max Suppression that de-duplicates "
            "each detector's own multi-scale boxes (intra-detector, not the SCRFD↔YOLO "
            "fusion). Typical 0.3-0.6; default 0.45. Lower it if one face gets "
            "duplicate/stacked boxes (common with multiple scales); raise it if two "
            "adjacent faces (cheek-to-cheek) collapse into one."
        ),
    )
    cross_iou: float = Field(
        default=0.50,
        description=(
            "IoU threshold for fusing SCRFD and YOLO detections: a YOLO box overlapping "
            "an SCRFD box above this is treated as the same face (the SCRFD box and its "
            "landmarks are kept); below it, the YOLO box is added as a new face. Typical "
            "0.3-0.6; default 0.50. Lower it if the same face appears twice after fusion "
            "(once per detector); raise it if two nearby distinct faces get merged into "
            "one. No effect without the YOLO model."
        ),
    )
    min_face_px: int = Field(
        default=20,
        description=(
            "Minimum face size (shorter box side, in original-image pixels) to keep; "
            "smaller detections are dropped. Typical 16-40; default 20. Tiny crops "
            "produce weak, noisy embeddings that pollute clustering, so raise it if "
            "clustering is degraded by garbage faces; lower it only if you genuinely "
            "need distant/background faces. Tune together with scales, which recovers "
            "small faces this filter may then discard."
        ),
    )
    max_long_side: int = Field(
        default=6000,
        description=(
            "Upper cap (pixels) on SCRFD's upscaled long side, bounding memory from the "
            "scales pyramid; an upscale factor is reduced so long_side × factor stays "
            "under this. It caps upscaling only — it does not shrink an already-large "
            "original. Typical 4000-8000, memory-bound; default 6000. Lower it on OOM or "
            "slow high-megapixel images; raise it if your scales include 2.0+ but small "
            "faces on large images still aren't recovered (the clamp is likely "
            "cancelling the upscale). SCRFD only."
        ),
    )
    yolo_upscale_below_px: int = Field(
        default=1600,
        description=(
            "Long-side threshold (pixels) below which YOLO does an extra 2× upscale pass "
            "(YOLO always letterboxes to 640px, so small images otherwise waste "
            "resolution). Typical 1000-2500; default 1600. Raise it if small faces in "
            "medium-resolution (~640-1600px) images are missed; lower it if YOLO latency "
            "dominates and small-face recall isn't needed. YOLO only; independent of "
            "SCRFD's scales."
        ),
    )


class RestorationConfig(BaseModel):
    enabled: bool = Field(
        default=False,
        description=(
            "Master switch for the optional CodeFormer face-restoration pass. "
            "Restoration is advisory only — it never feeds clustering; it fills the "
            "'restored' embedding variant and files restore_disagreement review flags. "
            "Off (default) = zero cost. On requires the [restore] extra (which pins old "
            "torch/torchvision and is incompatible with the GPU extra — CPU inference "
            "only) plus a vendored CodeFormer model; without those it fails at startup "
            "or on the first crop. Leave off unless you have set restoration up "
            "specifically as a QA aid."
        ),
    )
    trigger_px: int = Field(
        default=80,
        description=(
            "Restore any face whose shorter bounding-box side is below this many pixels, "
            "regardless of quality (OR'd with the quality gate). Default 80; practical "
            "40-160. The effective restoration band is roughly [detection.min_face_px, "
            "trigger_px). Raise it to restore mid-size faces (more compute, more "
            "hallucination risk on faces that were fine); lower it to restore only "
            "genuinely tiny crops. Watch which crops get restored (faces.restored=1 vs "
            "bbox size)."
        ),
    )
    quality_percentile: float = Field(
        default=15.0,
        description=(
            "Restore faces in the bottom N% of each batch by MagFace embedding norm "
            "(MagFace magnitude is the per-face quality signal). Default 15.0; range "
            "0-100. This is relative per-batch, not an absolute cut — a fixed fraction "
            "is always eligible even in an all-good batch, and 0 effectively disables "
            "the quality gate. Raise for more coverage (more compute/hallucination "
            "risk), lower to target only the worst faces. Evaluate on representative "
            "batches, since composition matters."
        ),
    )
    fidelity: float = Field(
        default=0.7,
        description=(
            "CodeFormer's fidelity weight w in [0,1]: higher stays closer to the input "
            "(preserves identity, less enhancement), lower is more generative "
            "(better-looking, higher risk of altering identity). Default 0.7 leans "
            "toward fidelity, appropriate for a recognition pipeline. This is the "
            "primary knob against over-restoration — if lowering it pushes many faces "
            "over disagreement_cos, CodeFormer is inventing identity, so raise it back. "
            "Safe to A/B since restored embeddings never feed clustering."
        ),
    )
    disagreement_cos: float = Field(
        default=0.30,
        description=(
            "Flag a restored face for human review when its restored-vs-original "
            "embedding disagreement (1 − cosine) exceeds this; 0.30 (default) ≈ cosine "
            "0.70 between the two embeddings. Practical band ~0.1-0.6 (theoretical max "
            "2.0). Lower it to catch subtle identity drift early (larger review queue); "
            "raise it to flag only gross changes. This is the direct 'did restoration "
            "hallucinate a different identity' detector — tune it together with fidelity "
            "and watch the restore_disagreement queue volume."
        ),
    )


class ClusteringConfig(BaseModel):
    knn_k: int = Field(
        default=64,
        description=(
            "Number of nearest neighbors retrieved per face when building the "
            "cosine-similarity kNN graph — an upper bound on connectivity, not a cluster "
            "size. Typical 20-128; default 64. Too low truncates neighbor lists below a "
            "prolific person's true neighbor count, causing fragmentation that no "
            "threshold can fix; too high adds cross-identity candidate edges and "
            "quadratic compute. Changing it (or fusion_weights) invalidates the graph "
            "cache and forces a full recompute, unlike edge_threshold. Raise until "
            "recall (bcubed_recall in `eval`) plateaus."
        ),
    )
    edge_threshold: float = Field(
        default=0.50,
        description=(
            "Cosine-similarity cutoff for keeping a graph edge in Chinese Whispers "
            "(ignored by HDBSCAN); faces with no surviving edge become singletons. Range "
            "0-1; sweet spot ~0.40-0.60; default 0.50. Too low lets weak/cross-identity "
            "edges chain distinct people into giant merged clusters; too high fragments "
            "identities into singletons on pose/age/lighting variation. This is the "
            "primary, cheapest-to-sweep knob (it doesn't invalidate the cache) — sweep "
            "via `recluster --set` or `eval grid-search` and watch the precision/recall "
            "(bcubed) tradeoff."
        ),
    )
    algorithm: Literal["chinese_whispers", "hdbscan"] = Field(
        default="chinese_whispers",
        description=(
            "Clustering routine. 'chinese_whispers' (default) is iterative "
            "label-propagation on the thresholded graph: fast, no extra dependency, "
            "tuned by edge_threshold — every face gets a label. 'hdbscan' is density "
            "clustering that actively rejects low-density faces as noise (higher "
            "precision, lower recall, more explicit singletons), tuned by "
            "min_cluster_size and needing the optional hdbscan dependency; it ignores "
            "edge_threshold and cw_iterations. Compare both on the same holdout with "
            "`eval grid-search` — the cache is reused, so the A/B is cheap."
        ),
    )
    cw_iterations: int = Field(
        default=30,
        description=(
            "Number of label-propagation sweeps in Chinese Whispers (no early stopping — "
            "it always runs this many). Typical 10-50; default 30 is over-provisioned. "
            "Too few leaves labels unconverged and identities under-merged; more than "
            "needed just wastes time (it does not cause over-merging — topology is fixed "
            "by edge_threshold). Tune manually with `recluster --set "
            "clustering.cw_iterations=N`: raise if assignments still shift between 30 and "
            "40, lower if identical from 15 up. HDBSCAN ignores it."
        ),
    )
    min_cluster_size: int = Field(
        default=2,
        description=(
            "HDBSCAN only — has no effect under the default chinese_whispers algorithm. "
            "Minimum faces for a cluster (connected components smaller than this are "
            "pruned to noise); also used as min_samples, so larger values label more "
            "faces as noise. Integer ≥ 2 (clamped up); default 2 maximizes recall. Raise "
            "to 3-5 for higher purity at the cost of losing real small identities "
            "(people with only 2-3 photos) to noise. For the minimum size to propose a "
            "brand-new person, see crossref.new_person_min_faces instead."
        ),
    )
    seed: int = Field(
        default=42,
        description=(
            "RNG seed for Chinese Whispers' per-iteration node visit order, making runs "
            "byte-identical given the same inputs (HDBSCAN doesn't use it). Not a quality "
            "knob — keep it fixed (default 42) for reproducible review queues and "
            "comparable grid searches. Only vary it to probe robustness: large swings in "
            "cluster assignments across seeds indicate an over-connected graph near a "
            "percolation point, so raise edge_threshold rather than hunting for a 'good' "
            "seed."
        ),
    )
    fusion_weights: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Per-model weights (keyed by embedding model name, e.g. "
            "arcface/adaface/magface; a missing model = 1.0) controlling each model's "
            "relative contribution to the fused cosine similarity. Each model block is "
            "L2-normalized first, so a model's share of the final similarity scales with "
            "weight²; only ratios matter. Empty (default) weights all models equally. Set "
            "a weight toward 0 to suppress a model that is noisy on your data; "
            "over-weighting one model discards the ensemble benefit. Changing any weight "
            "invalidates the graph cache; tune manually with `recluster --set` + `eval "
            "holdout` (bcubed_f1), and make sure names match the embeddings table exactly."
        ),
    )


class CrossrefConfig(BaseModel):
    majority: float = Field(
        default=0.60,
        description=(
            "Fraction of a cluster's *labeled* faces that must agree on one Synology "
            "person before the cluster is trusted as that person's (paired with "
            "min_labeled). This mapping gates every downstream suggestion — assign, "
            "reassign, and cluster-pair merges. Range ~0.50-0.80; default 0.60 tolerates "
            "one dissenting label at min_labeled=3. Raise it if wrong-person assigns "
            "appear (impure clusters mapping); lower it if coherent, well-labeled "
            "clusters produce no assigns. Too low also inflates false merges."
        ),
    )
    min_labeled: int = Field(
        default=3,
        description=(
            "Minimum number of labeled (Synology-tagged) faces a cluster needs before "
            "its majority vote is trusted at all; below this the cluster is ignored (no "
            "assign/merge). Also the leave-one-out support floor for reassigns. Sensible "
            "2-6; default 3. With min_labeled=1 a single tagged face maps the whole "
            "cluster (fragile). Raise it if clusters get mapped/assigned off one or two "
            "faces that turn out wrong; lower toward 2 if your library is sparsely "
            "tagged and real clusters rarely reach 3 labels."
        ),
    )
    assign_sim: float = Field(
        default=0.55,
        description=(
            "Cosine-similarity cutoff separating a confident 'assign' from a "
            "'low_confidence' flag: an unlabeled face's mean similarity to the cluster's "
            "labeled faces at/above this is an assign, below it is queued as "
            "low_confidence (both still reviewable). Also the floor below which reassign "
            "claims are discarded as noise. Range [-1,1]; practical 0.40-0.70; default "
            "0.55. This is the confidence score shown in the review UI. Raise it if "
            "wrong faces land in 'assign'; lower it if many correct faces are stuck in "
            "'low_confidence'."
        ),
    )
    new_person_min_faces: int = Field(
        default=5,
        description=(
            "Minimum size of a fully-unlabeled cluster (zero Synology matches) before it "
            "is proposed as a brand-new person. Default 5; sensible 3-15. This is a size "
            "floor, not a similarity — it filters small, likely-spurious clusters "
            "(noise, one-off detections). Raise it if the new_person queue is full of "
            "junk (blurry/partial faces, single-event strangers); lower it if genuine "
            "recurring people who appear in few photos are missed. Quality here depends "
            "on upstream clustering coherence."
        ),
    )
    merge_vote_fraction: float = Field(
        default=0.30,
        description=(
            "First merge trigger (intra-cluster): when two different Synology persons "
            "each hold at least this share of the *same* cluster, they're suggested as "
            "the same identity Synology split. Fraction ~0.20-0.50; default 0.30 (below "
            "0.5, so a cluster can nominate more than two co-dominant persons). Raise it "
            "for fewer, more confident merge suggestions; lower it to catch more Synology "
            "over-splits at the cost of false merges from a few stray mislabeled faces. "
            "Watch the merge/merge_named queue for false merges."
        ),
    )
    merge_centroid_sim: float = Field(
        default=0.60,
        description=(
            "Second merge trigger (inter-cluster): two clusters that mapped to "
            "*different* persons but whose mean-embedding centroids are closer than this "
            "cosine are suggested as the same person split across clusters. Range "
            "[-1,1]; practical 0.50-0.75; default 0.60 (centroid-to-centroid similarity "
            "runs lower than face-to-face). Raise it so only near-identical clusters "
            "merge; lower it if one person is clearly fragmented across clusters that "
            "never get proposed. Tune together with merge_vote_fraction and judge by the "
            "combined false-merge rate. Both merge kinds still require explicit "
            "apply-time gates before anything is written."
        ),
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
