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
    data_dir: Path = Path("/data")
    models_dir: Path = Path("/models")
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
    device: Literal["auto", "cpu", "cuda"] = "auto"
    batch_size: int = 16
    intra_op_threads: int | None = None  # None = physical cores


class DetectionConfig(BaseModel):
    scales: list[float] = Field(default_factory=lambda: [1.0, 2.0])
    scrfd_score: float = 0.30
    yolo_score: float = 0.35
    nms_iou: float = 0.45
    cross_iou: float = 0.50
    min_face_px: int = 20
    max_long_side: int = 6000
    yolo_upscale_below_px: int = 1600


class RestorationConfig(BaseModel):
    enabled: bool = False
    trigger_px: int = 80
    quality_percentile: float = 15.0
    fidelity: float = 0.7  # CodeFormer w
    disagreement_cos: float = 0.30


class ClusteringConfig(BaseModel):
    knn_k: int = 64
    edge_threshold: float = 0.50
    algorithm: Literal["chinese_whispers", "hdbscan"] = "chinese_whispers"
    cw_iterations: int = 30
    min_cluster_size: int = 2
    seed: int = 42
    # Per-model fusion weights; missing model -> weight 1.0.
    fusion_weights: dict[str, float] = Field(default_factory=dict)


class CrossrefConfig(BaseModel):
    majority: float = 0.60
    min_labeled: int = 3
    assign_sim: float = 0.55
    new_person_min_faces: int = 5
    merge_vote_fraction: float = 0.30
    merge_centroid_sim: float = 0.60


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SYNOPTICON_",
        env_nested_delimiter="__",
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
        # Earlier sources win: init > env > toml.
        return (
            init_settings,
            env_settings,
            TomlConfigSettingsSource(settings_cls, toml_file=_config_file()),
        )


def _config_file() -> Path | None:
    explicit = os.environ.get("SYNOPTICON_CONFIG")
    if explicit:
        return Path(explicit)
    for candidate in (Path("config.toml"), Path("/data/config.toml")):
        if candidate.is_file():
            return candidate
    return None


def load_settings(**overrides) -> Settings:
    return Settings(**overrides)
