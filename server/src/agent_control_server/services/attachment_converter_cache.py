"""The key a conversion is cached under. The cache itself belongs to the caller.

Conversion cannot run inline: five visuals on one issue is roughly a hundred
seconds of OCR against a twenty-five second per-step budget. So the work runs
out of band and a step reads a stored result instead of waiting for one, and
that arrangement needs exactly one thing from this library - a key that is the
same for the same content under the same conditions, and different when the
answer would be.

What is stored under the key, where, for how long, and what a miss renders in
front of an agent are all the caller's. This module is the contract between the
two halves and it is deliberately the whole of it.
"""

from __future__ import annotations

import hashlib

from agent_control_models.attachment_converter import DEFAULT_OPTIONS, ConversionOptions
from agent_control_models.attachment_converter_backends import ConverterBackend, default_backends

CONVERSION_CONTRACT_VERSION = 1
"""Bumped when a change to the conversion pipeline makes a cached result wrong.

Part of every key, so one increment retires every stored conversion without a
migration and without anybody hunting for stale rows."""


def conversion_cache_key(
    source_sha256: str,
    *,
    options: ConversionOptions = DEFAULT_OPTIONS,
    backends: tuple[ConverterBackend, ...] | None = None,
) -> str:
    """Return the cache key for converting this content under these conditions.

    Content decides most of it. The rest is what would change the answer for
    identical content: the contract version, the options, and **which
    converters are installed**.

    That last one is the decision worth stating. Installing Docling turns every
    zero-character PNG into 552 characters of OCR, so a key that ignored
    availability would go on serving the empty answer forever, on the corpus
    where five of six files depend on it. Converter *versions* are deliberately
    excluded: a point upgrade would retire every entry at once and re-OCR the
    whole corpus at twenty seconds a file, which buys a marginally better
    extraction at the cost of the thing the cache exists to prevent. Bump
    :data:`CONVERSION_CONTRACT_VERSION` on the day an upgrade is worth that.

    The key carries no content and no filename. It is a hash of a hash and a
    handful of settings, so it is safe in a log line and safe in a URL.
    """
    active = default_backends() if backends is None else backends
    available = ",".join(sorted(b.name for b in active if b.available()))
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
    return f"acv{CONVERSION_CONTRACT_VERSION}:{digest}"
