"""The key a conversion is cached under. The cache itself belongs to the caller.

Conversion cannot run inline: five visuals on one issue is roughly a hundred
seconds of OCR against a twenty-five second per-step budget. So the work runs
out of band and a step reads a stored result instead of waiting for one, and
that arrangement needs exactly one thing from this library - a key that is the
same for the same content under the same conditions, and different when the
answer would be.

What is stored under the key, where, for how long, and what a miss renders in
front of an agent are all the caller's. This module is the contract between the
two halves and it is deliberately the whole of it - two names: the key, which
decides where an answer lives, and the capability fingerprint, which decides
whether a stored *failure* still describes this deployment.
"""

from __future__ import annotations

import hashlib

from agent_control_models.attachment_converter import DEFAULT_OPTIONS, ConversionOptions
from agent_control_models.attachment_converter_backends import (
    ConverterBackend,
    default_backends,
    installed_format_support,
)

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
    available = _available_backend_names(active)
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


def _available_backend_names(backends: tuple[ConverterBackend, ...]) -> str:
    return ",".join(sorted(b.name for b in backends if b.available()))


def installed_capability_fingerprint(
    backends: tuple[ConverterBackend, ...] | None = None,
) -> str:
    """Name the installed capability set, finely enough for a failure to cite.

    The key above already folds in which backends report ``available()``, so a
    whole converter appearing rotates every key and the stale rows are simply
    never found again. What the key cannot see is a capability arriving
    *inside* an installed backend: MarkItDown without its pptx extra answers
    ``available()`` identically before and after the rebuild that adds it, the
    key holds still, and a failure stored as ``ocr_converter_absent`` keeps
    answering for a deck the deployment can now read. That happened to a real
    deck and took a hand-written DELETE to clear.

    So the fingerprint is the key's availability list plus the per-format
    support modules, and it rides on the stored row rather than in the key. In
    the key it would rotate every *successful* conversion too, re-running OCR
    on a corpus the new extra cannot have changed; on the row it retires
    exactly the verdicts the change could overturn - the caller treats a
    failed row bearing a different fingerprint as a miss, and everything else
    stands.

    Stored readable rather than hashed, because the question it answers - what
    was installed when this verdict was written - is one an operator asks
    while staring at a failed row in ``psql``.
    """
    active = default_backends() if backends is None else backends
    return "|".join(
        (
            f"v{CONVERSION_CONTRACT_VERSION}",
            _available_backend_names(active),
            ",".join(installed_format_support()),
        )
    )
