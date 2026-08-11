"""The file source: a YAML list of items, section 14 slice 1."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import yaml

from .base import SourceItem, WriteBackOutcome

_FILE_SCHEME = "file://"


class SourceParseError(ValueError):
    """The source file is not something this can dispatch from."""


def resolve_source(spec: str) -> FileTaskSource:
    """Build a file source from a ``--source`` argument."""

    if spec.startswith(_FILE_SCHEME):
        return FileTaskSource(Path(spec[len(_FILE_SCHEME) :]).expanduser())
    if "://" in spec:
        scheme = spec.split("://", 1)[0]
        raise SourceParseError(
            f"Unsupported source scheme '{scheme}://'. Files are file://; one Linear "
            "milestone is '--source linear-milestone:<id> --team <slug>'. Any wider "
            "Linear source, and any write back to Linear, is a later phase and does "
            "not exist yet."
        )
    return FileTaskSource(Path(spec).expanduser())


class FileTaskSource:
    """A YAML file of items, read whole on every poll."""

    kind = "file"

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def describe(self) -> str:
        return f"file://{self._path}"

    async def poll(self, *, cursor: str | None) -> list[SourceItem]:
        """Items eligible for claiming, oldest first."""

        items = _load(self._path)
        if cursor is None:
            return items
        refs = [item.ref for item in items]
        if cursor not in refs:
            return items
        return items[refs.index(cursor) + 1 :]

    async def write_back(
        self, *, item_ref: str, body: str, idempotency_marker: str
    ) -> WriteBackOutcome:
        """Not in this slice, and not faked."""

        raise NotImplementedError(
            "The file source has no write-back. Slice 1 does not write to the source; "
            "the operator reads the transcript."
        )


def _load(path: Path) -> list[SourceItem]:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourceParseError(f"Cannot read source file {path}: {exc}") from exc

    try:
        parsed = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise SourceParseError(f"{path} is not valid YAML: {exc}") from exc

    if parsed is None:
        return []
    if not isinstance(parsed, list):
        raise SourceParseError(
            f"{path} must be a YAML list of items; found {type(parsed).__name__}."
        )

    items = [_item(entry, index=index, path=path) for index, entry in enumerate(parsed)]
    _reject_duplicate_refs(items, path=path)

    if items and all(item.updated_at is not None for item in items):
        items.sort(key=_ordering_key)
    return items


def _ordering_key(item: SourceItem) -> dt.datetime:
    """Naive and aware timestamps in one file must not crash the sort."""

    moment = item.updated_at or dt.datetime.min
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=dt.UTC)


def _item(entry: Any, *, index: int, path: Path) -> SourceItem:
    where = f"{path} item {index}"
    if not isinstance(entry, dict):
        raise SourceParseError(f"{where}: expected a mapping, found {type(entry).__name__}.")

    unknown = set(entry) - {"ref", "title", "body", "url", "updated_at"}
    if unknown:
        raise SourceParseError(
            f"{where}: unknown keys {sorted(unknown)}. An item carries ref, title, body "
            "and optionally url and updated_at. Nothing else reaches a decision "
            "(plan section 5.1)."
        )

    ref = entry.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        raise SourceParseError(f"{where}: 'ref' is required and must be a non-empty string.")

    title = _text(entry.get("title"), field="title", where=where)
    body = _text(entry.get("body"), field="body", where=where)
    if not title.strip() and not body.strip():
        raise SourceParseError(
            f"{where}: has neither a title nor a body. There is nothing to ask an agent to do."
        )

    url = entry.get("url")
    if url is not None and not isinstance(url, str):
        raise SourceParseError(f"{where}: 'url' must be a string when present.")

    return SourceItem(
        ref=ref.strip(),
        title=title,
        body=body,
        url=url,
        updated_at=_updated_at(entry.get("updated_at"), where=where),
    )


def _text(value: Any, *, field: str, where: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    raise SourceParseError(f"{where}: '{field}' must be a string when present.")


def _updated_at(value: Any, *, where: str) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time.min)
    raise SourceParseError(f"{where}: 'updated_at' must be a date or timestamp when present.")


def _reject_duplicate_refs(items: list[SourceItem], *, path: Path) -> None:
    seen: set[str] = set()
    for item in items:
        if item.ref in seen:
            raise SourceParseError(
                f"{path}: duplicate ref '{item.ref}'. Refs key the claim ledger, so a "
                "duplicate means one of the two items is silently never run."
            )
        seen.add(item.ref)
