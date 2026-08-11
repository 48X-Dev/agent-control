"""Converted markdown on the corpus database, under the attachment path's key shape.

An optimisation and never a dependency: every failure in here converts instead.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

import sqlalchemy as sa
from agent_control_models.attachment_converter import DEFAULT_OPTIONS, ConversionOptions
from agent_control_models.attachment_converter_backends import ConverterBackend, default_backends
from sqlalchemy.engine import Engine

from .convert import INDEXABLE_STATUSES, Converted, install_conversion_cache

__all__ = [
    "CONVERSION_CONTRACT_VERSION",
    "NORMALIZER_VERSION",
    "RETENTION_DAYS",
    "CorpusConversionCache",
    "conversion_cache_key",
    "open_conversion_cache",
]

CONVERSION_CONTRACT_VERSION = 1
"""The attachment path's version, mirrored here because the sync image has no server."""

NORMALIZER_VERSION = 1
"""Bumped when a change to ``normalize_for_chunking`` makes stored markdown wrong."""

RETENTION_DAYS = 30
"""How long a conversion nothing has read survives."""

logger = logging.getLogger(__name__)

_TOUCH = sa.text(
    "UPDATE conversion_cache SET last_used_at = now() WHERE key = :key "
    "RETURNING status, error_code, body"
)

_STORE = sa.text(
    """
    INSERT INTO conversion_cache (key, status, error_code, body, stored_at, last_used_at)
         VALUES (:key, :status, :error_code, :body, now(), now())
    ON CONFLICT (key) DO UPDATE
            SET status = excluded.status,
                error_code = excluded.error_code,
                body = excluded.body,
                stored_at = excluded.stored_at,
                last_used_at = excluded.last_used_at
    """
)

_FORGET = sa.text("DELETE FROM conversion_cache WHERE key = :key")

_SWEEP = sa.text(
    "DELETE FROM conversion_cache WHERE last_used_at < now() - make_interval(days => :days)"
)


def conversion_cache_key(
    source_sha256: str,
    *,
    options: ConversionOptions = DEFAULT_OPTIONS,
    backends: tuple[ConverterBackend, ...] | None = None,
) -> str:
    """The attachment path's key for this content, with the sync's normalizer on it."""

    active = default_backends() if backends is None else backends
    return _key(source_sha256, options=options, available=available_backends(active))


def available_backends(backends: Sequence[ConverterBackend]) -> str:
    """The installed converters, as the key spells them. Probing costs a spec lookup each."""

    return ",".join(sorted(b.name for b in backends if b.available()))


def _key(source_sha256: str, *, options: ConversionOptions, available: str) -> str:
    fingerprint = "|".join(
        (
            f"v{CONVERSION_CONTRACT_VERSION}",
            source_sha256,
            ",".join(sorted(options.accepted_mimes)),
            str(options.low_text_threshold_chars),
            str(options.text_max_chars),
            "ocr" if options.allow_ocr else "no-ocr",
            available,
        )
    )
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    return f"ksn{NORMALIZER_VERSION}:acv{CONVERSION_CONTRACT_VERSION}:{digest}"


def is_empty_success(converted: Converted) -> bool:
    """A status that claims text with no text behind it, which must never be stored."""

    return converted.status in INDEXABLE_STATUSES and not converted.text.strip()


class CorpusConversionCache:
    """Conversions kept beside the corpus they were converted for."""

    def __init__(self, engine: Engine, *, retention_days: int = RETENTION_DAYS) -> None:
        self._engine = engine
        self._retention = retention_days
        self._available = available_backends(default_backends())
        self._live = True

    @property
    def live(self) -> bool:
        """False once the database refused; every later call is a no-op."""

        return self._live

    def key_for(self, data: bytes) -> str:
        """This document's key, with the backend probe done once per process."""

        digest = hashlib.sha256(data).hexdigest()
        return _key(digest, options=DEFAULT_OPTIONS, available=self._available)

    def get(self, key: str) -> Converted | None:
        """A stored conversion, marked used in the same statement, or None for a miss."""

        row = self._one(_TOUCH, {"key": key})
        if row is None:
            return None
        found = Converted(text=row.body, status=row.status, error_code=row.error_code)
        if is_empty_success(found):
            self._run(_FORGET, {"key": key})
            return None
        return found

    def put(self, key: str, converted: Converted) -> None:
        """Store one outcome verbatim, success or failure, unless it is an empty success."""

        if is_empty_success(converted):
            return
        self._run(
            _STORE,
            {
                "key": key,
                "status": converted.status,
                "error_code": converted.error_code,
                "body": converted.text,
            },
        )

    def sweep(self) -> int:
        """Drop conversions nothing has read inside the retention window."""

        result = self._run(_SWEEP, {"days": self._retention})
        return 0 if result is None else int(result)

    def close(self) -> None:
        self._engine.dispose()

    def _one(self, statement: sa.TextClause, params: Mapping[str, Any]) -> Any:
        if not self._live:
            return None
        try:
            with self._engine.begin() as conn:
                return conn.execute(statement, params).first()
        except Exception:
            self._give_up()
            return None

    def _run(self, statement: sa.TextClause, params: Mapping[str, Any]) -> int | None:
        if not self._live:
            return None
        try:
            with self._engine.begin() as conn:
                return conn.execute(statement, params).rowcount
        except Exception:
            self._give_up()
            return None

    def _give_up(self) -> None:
        """One failure ends the cache for this process rather than per document."""

        self._live = False
        logger.warning("conversion cache is unreachable; converting everything", exc_info=True)


@contextmanager
def open_conversion_cache(
    database_url: str,
    *,
    retention_days: int = RETENTION_DAYS,
) -> Iterator[CorpusConversionCache | None]:
    """Install the cache for one run, sweeping on the way in and letting go on the way out."""

    cache = _open(database_url, retention_days=retention_days)
    previous = install_conversion_cache(cache)
    try:
        yield cache
    finally:
        install_conversion_cache(previous)
        if cache is not None:
            cache.close()


def _open(database_url: str, *, retention_days: int) -> CorpusConversionCache | None:
    """Build the cache, or nothing at all if this database will not answer."""

    try:
        engine = sa.create_engine(database_url, future=True)
    except Exception:
        logger.warning("conversion cache could not be opened; converting everything", exc_info=True)
        return None
    cache = CorpusConversionCache(engine, retention_days=retention_days)
    # The sweep doubles as the reachability probe.
    cache.sweep()
    if cache.live:
        return cache
    cache.close()
    return None
