"""Typed wrappers over SynoClient for Browse.Item / Browse.Person / Download.

Every function takes an explicit `space` so callers can work against both
`personal` and `shared` (FotoTeam) libraries with the same code path.
"""

from __future__ import annotations

from typing import Iterable, Iterator

from synopticon.config import Space
from synopticon.syno.client import QuotedString, SynoClient
from synopticon.syno.models import ItemSummary, Person, SimilarGroup, SynoFace

DEFAULT_ITEM_ADDITIONAL: tuple[str, ...] = ("thumbnail", "resolution", "orientation", "person")
DEFAULT_PERSON_ADDITIONAL: tuple[str, ...] = ("thumbnail",)


def list_items(
    client: SynoClient,
    space: Space,
    additional: Iterable[str] = DEFAULT_ITEM_ADDITIONAL,
    page_size: int = 500,
) -> Iterator[ItemSummary]:
    """Paginated full-library item listing (Browse.Item.list)."""
    api = client.api_name(space, "Browse.Item")
    version = client.version_for(api, 7)
    for raw in client.paginate(
        api, "list", page_size=page_size, version=version, additional=list(additional)
    ):
        yield ItemSummary.from_api(space, raw)


def get_item(
    client: SynoClient, space: Space, item_id: int, additional: Iterable[str] = ("person",)
) -> ItemSummary:
    api = client.api_name(space, "Browse.Item")
    version = client.version_for(api, 7)
    data = client.call(api, "get", version=version, id=[item_id], additional=list(additional))
    items = data.get("list") or []
    if not items:
        raise LookupError(f"item {item_id} not found in space {space!r}")
    return ItemSummary.from_api(space, items[0])


def list_persons(
    client: SynoClient,
    space: Space,
    show_hidden: bool = True,
    page_size: int = 500,
    show_more: bool = False,
) -> Iterator[Person]:
    """Paginated full-library person listing (Browse.Person.list).

    `show_more=True` is Synology Photos' "show more people" listing: it includes
    the long tail of low-item-count (usually unnamed) people the default view
    hides. QuickMerger needs it; the sync pass deliberately does not.
    """
    api = client.api_name(space, "Browse.Person")
    version = client.version_for(api, 1)
    for raw in client.paginate(
        api,
        "list",
        page_size=page_size,
        version=version,
        additional=list(DEFAULT_PERSON_ADDITIONAL),
        show_more=show_more,
        show_hidden=show_hidden,
    ):
        yield Person.from_api(space, raw)


def get_person(
    client: SynoClient,
    space: Space,
    person_id: int,
    additional: Iterable[str] = DEFAULT_PERSON_ADDITIONAL,
) -> Person:
    api = client.api_name(space, "Browse.Person")
    version = client.version_for(api, 1)
    data = client.call(api, "get", version=version, id=[person_id], additional=list(additional))
    items = data.get("list") or []
    if not items:
        raise LookupError(f"person {person_id} not found in space {space!r}")
    return Person.from_api(space, items[0])


def suggest_person(
    client: SynoClient,
    space: Space,
    name_prefix: str,
    limit: int = 10,
    additional: Iterable[str] = DEFAULT_PERSON_ADDITIONAL,
) -> list[Person]:
    """Browse.Person.suggest — name-prefix autocomplete, not paginated (flat, `limit`-capped)."""
    api = client.api_name(space, "Browse.Person")
    version = client.version_for(api, 3)
    data = client.call(
        api,
        "suggest",
        version=version,
        name_prefix=QuotedString(name_prefix),
        additional=list(additional),
        limit=limit,
    )
    return [Person.from_api(space, raw) for raw in (data.get("list") or [])]


def list_item_faces(
    client: SynoClient, space: Space, item_id: int, additional: Iterable[str] = ("thumbnail",)
) -> list[SynoFace]:
    """Browse.Item.list_face — flat (not paginated) list of faces on one photo."""
    api = client.api_name(space, "Browse.Item")
    version = client.version_for(api, 7)
    data = client.call(api, "list_face", version=version, id_item=item_id, additional=list(additional))
    return [SynoFace.from_api(space, item_id, raw) for raw in (data.get("list") or [])]


def list_similar_groups(
    client: SynoClient, space: Space, page_size: int = 500
) -> Iterator[SimilarGroup]:
    """Paginated "similar photo group" (stacking) listing (Browse.SimilarItem.list).

    Non-top-pick group members are omitted from the response entirely; only
    the top-pick row of each group carries a `similar` key, which is what this
    yields one `SimilarGroup` per. Rows without it (ungrouped photos) are
    skipped -- there is nothing to record for them.
    """
    api = client.api_name(space, "Browse.SimilarItem")
    version = client.version_for(api, 2)
    for raw in client.paginate(api, "list", page_size=page_size, version=version):
        if "similar" in raw:
            yield SimilarGroup.from_api(raw)


def download_person_thumbnail(
    client: SynoClient, space: Space, person_id: int, cache_key: str
) -> Iterator[bytes]:
    """Stream a person's cover thumbnail (Thumbnail.get, `type="person"`).

    Param shape is the one the Synology Photos web UI itself uses: `api`,
    `method` and `type` quoted, `cache_key` quoted, `id` bare, no `size` —
    deviating from it (adding `size`) is untested against the live NAS.
    """
    api = client.api_name(space, "Thumbnail")
    version = client.version_for(api, 2)
    yield from client.stream(
        api,
        "get",
        version=version,
        quote_api=True,
        id=person_id,
        cache_key=QuotedString(cache_key),
        type=QuotedString("person"),
    )


def download_original(client: SynoClient, space: Space, unit_id: int, cache_key: str) -> Iterator[bytes]:
    """Stream the original (full-resolution) bytes for one item.

    Matches the captured v2 GET form byte-for-byte: `api`/`method` themselves
    quoted, `cache_key` quoted, `unit_id` a bare JSON array.
    """
    api = client.api_name(space, "Download")
    version = client.version_for(api, 2)
    yield from client.stream(
        api,
        "download",
        version=version,
        quote_api=True,
        cache_key=QuotedString(cache_key),
        unit_id=[unit_id],
    )
