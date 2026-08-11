"""The key a conversion is cached under. The cache itself belongs to the caller."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from .attachment_converter import DEFAULT_OPTIONS, ConversionOptions
from .attachment_converter_backends import (
    ConverterBackend,
    default_backends,
    installed_format_support,
)

CONVERSION_CONTRACT_VERSION = 1
"""Bumped when a change to the conversion pipeline makes a cached result wrong.

Part of every key, so one increment retires every stored conversion without a
migration and without anybody hunting for stale rows."""


def available_backends(backends: Sequence[ConverterBackend] | None = None) -> str:
    """The installed converters, as the key spells them. Each probe is a spec lookup."""
    active = default_backends() if backends is None else backends
    return ",".join(sorted(b.name for b in active if b.available()))


def conversion_cache_key(
    source_sha256: str,
    *,
    options: ConversionOptions = DEFAULT_OPTIONS,
    backends: tuple[ConverterBackend, ...] | None = None,
    available: str | None = None,
) -> str:
    """Return the cache key for converting this content under these conditions."""
    probed = available_backends(backends) if available is None else available
    fingerprint = "|".join(
        (
            f"v{CONVERSION_CONTRACT_VERSION}",
            source_sha256,
            ",".join(sorted(options.accepted_mimes)),
            str(options.low_text_threshold_chars),
            str(options.text_max_chars),
            "ocr" if options.allow_ocr else "no-ocr",
            probed,
        )
    )
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    return f"acv{CONVERSION_CONTRACT_VERSION}:{digest}"


def installed_capability_fingerprint(
    backends: tuple[ConverterBackend, ...] | None = None,
) -> str:
    """Name the installed capability set, finely enough for a failure to cite."""
    return "|".join(
        (
            f"v{CONVERSION_CONTRACT_VERSION}",
            available_backends(backends),
            ",".join(installed_format_support()),
        )
    )
