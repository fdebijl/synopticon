"""Typed payload models for the Synology Photos API.

Shapes mirror the captured requests/responses in requests.md and
adding_face_to_photo_without_face.har. Bounding boxes are normalized [0-1]
with the same top_left/bottom_right structure Synology uses on the wire.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from synopticon.config import Space


@dataclass(frozen=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True)
class BBox:
    """Normalized [0-1] face bounding box, matching `face_bounding_box`."""

    top_left: Point
    bottom_right: Point

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> BBox:
        return cls(
            top_left=Point(x=raw["top_left"]["x"], y=raw["top_left"]["y"]),
            bottom_right=Point(x=raw["bottom_right"]["x"], y=raw["bottom_right"]["y"]),
        )

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.top_left.x, self.top_left.y, self.bottom_right.x, self.bottom_right.y)


@dataclass(frozen=True)
class Person:
    id: int
    space: Space
    name: str | None
    item_count: int | None
    show: bool | None
    cover: int | None
    #: `additional.thumbnail.cache_key`, present only when the caller asked for
    #: `additional=["thumbnail"]`. Required to build a Thumbnail.get URL.
    thumbnail_cache_key: str | None = None

    @classmethod
    def from_api(cls, space: Space, raw: dict[str, Any]) -> Person:
        thumbnail = (raw.get("additional") or {}).get("thumbnail") or {}
        return cls(
            id=raw["id"],
            space=space,
            name=raw.get("name"),
            item_count=raw.get("item_count"),
            show=raw.get("show"),
            cover=raw.get("cover"),
            thumbnail_cache_key=thumbnail.get("cache_key"),
        )


@dataclass(frozen=True)
class ItemSummary:
    id: int
    space: Space
    filename: str | None
    filesize: int | None
    folder_id: int | None
    time: int | None
    indexed_time: int | None
    type: str | None
    cache_key: str | None
    unit_id: int | None
    width: int | None
    height: int | None
    orientation: int | None
    person_ids: list[int] = field(default_factory=list)

    @classmethod
    def from_api(cls, space: Space, raw: dict[str, Any]) -> ItemSummary:
        additional = raw.get("additional") or {}
        thumbnail = additional.get("thumbnail") or {}
        resolution = additional.get("resolution") or {}
        persons = additional.get("person") or []
        return cls(
            id=raw["id"],
            space=space,
            filename=raw.get("filename"),
            filesize=raw.get("filesize"),
            folder_id=raw.get("folder_id"),
            time=raw.get("time"),
            indexed_time=raw.get("indexed_time"),
            type=raw.get("type"),
            cache_key=thumbnail.get("cache_key"),
            unit_id=thumbnail.get("unit_id"),
            width=resolution.get("width"),
            height=resolution.get("height"),
            orientation=additional.get("orientation"),
            person_ids=[p["id"] for p in persons],
        )


@dataclass(frozen=True)
class SynoFace:
    """A face known to Synology on one photo (from Browse.Item.list_face)."""

    space: Space
    photo_id: int
    face_id: int
    person_id: int | None
    name: str | None
    bbox: BBox

    @classmethod
    def from_api(cls, space: Space, photo_id: int, raw: dict[str, Any]) -> SynoFace:
        return cls(
            space=space,
            photo_id=photo_id,
            face_id=raw["face_id"],
            person_id=raw.get("person_id"),
            name=raw.get("name"),
            bbox=BBox.from_api(raw["face_bounding_box"]),
        )


@dataclass(frozen=True)
class SimilarGroup:
    """A Synology "similar photo group" (stacking), from Browse.SimilarItem.list.

    Only the top_pick row of a group carries the `similar` key on the wire;
    `item_ids` is the full membership (including the top pick itself).
    """

    id: int
    top_pick: int
    item_ids: list[int]

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> SimilarGroup:
        similar = raw["similar"]
        return cls(
            id=similar["id"],
            top_pick=similar["top_pick"],
            item_ids=list(similar["item_id"]),
        )


@dataclass(frozen=True)
class WriteResult:
    success: bool
    api: str
    method: str
    request_params: dict[str, Any]
    response: dict[str, Any] | None
    error_code: int | None = None
