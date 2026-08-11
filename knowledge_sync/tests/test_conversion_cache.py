"""What the cache keys on, what it refuses to keep, and what it lets go of."""

from __future__ import annotations

import ast
import dataclasses
import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from agent_control_knowledge_sync import convert as convert_module
from agent_control_knowledge_sync.conversion_cache import (
    NORMALIZER_VERSION,
    CorpusConversionCache,
    conversion_cache_key,
    open_conversion_cache,
)
from agent_control_knowledge_sync.convert import (
    Converted,
    convert_document,
    install_conversion_cache,
    installed_conversion_cache,
)
from agent_control_models.attachment_converter import DEFAULT_OPTIONS
from agent_control_models.attachment_converter_backends import default_backends
from agent_control_models.attachment_converter_cache import (
    conversion_cache_key as attachment_cache_key,
)
from sqlalchemy.pool import NullPool

from tests.conftest import execute, query, scalar

# Not a passthrough type, so the conversion path runs. Every test here stands the
# converter in, so no real parser ever sees these bytes.
DOCUMENT = b"PK\x03\x04 an uploaded deck"
OFFICE_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

MARKDOWN = "# Laptops\n\nOrdered on the first day, collected from the third floor."

# Nothing listens on port 1, so this refuses immediately rather than hanging.
UNREACHABLE = "postgresql+psycopg://nobody:nobody@127.0.0.1:1/agent_knowledge"

CACHED_ROWS = "SELECT key, status, error_code, body, last_used_at FROM conversion_cache"


class CountingConverter:
    """A stand-in for the shipped converter that says how often it actually ran."""

    def __init__(self, result: Converted) -> None:
        self.result = result
        self.calls = 0

    def __call__(self, data: bytes, *, declared_mime: str | None) -> Converted:
        self.calls += 1
        return self.result


@pytest.fixture()
def cache(corpus: Any) -> Iterator[CorpusConversionCache]:
    """An empty cache on the migrated corpus, disposed after the test."""
    execute(corpus, "DELETE FROM conversion_cache")
    engine = sa.create_engine(corpus.sync_url, future=True, poolclass=NullPool)
    store = CorpusConversionCache(engine)
    try:
        yield store
    finally:
        store.close()


@pytest.fixture()
def installed(cache: CorpusConversionCache) -> Iterator[CorpusConversionCache]:
    """The same cache, installed as the one this process converts through."""
    previous = install_conversion_cache(cache)
    try:
        yield cache
    finally:
        install_conversion_cache(previous)


@pytest.fixture()
def shipped(monkeypatch: pytest.MonkeyPatch) -> CountingConverter:
    """The shipped converter, counted. The cache only ever wraps this one."""
    counting = CountingConverter(Converted(text=MARKDOWN, status="text_layer_extracted"))
    monkeypatch.setattr(convert_module, "shipped_converter", counting)
    return counting


# --- The key ---------------------------------------------------------------


def test_the_key_is_the_attachment_paths_key_under_the_syncs_own_generation() -> None:
    """The generation prefix is the only thing the sync adds to the imported recipe."""
    digest = hashlib.sha256(DOCUMENT).hexdigest()

    assert conversion_cache_key(digest) == f"ksn{NORMALIZER_VERSION}:{attachment_cache_key(digest)}"


def test_no_shipped_module_reaches_for_the_server_package() -> None:
    """The sync image installs models and this package, so such an import is an ImportError.

    The key recipe moved to models for this reason, and spelling it a second
    time here is the other way to satisfy the constraint. This stops both.
    """
    package = Path(convert_module.__file__).parent

    offenders = []
    for source in sorted(package.rglob("*.py")):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name.startswith("agent_control_server") for name in names):
                offenders.append(f"{source.name}:{node.lineno}")

    assert not offenders


@pytest.mark.parametrize(
    "options",
    [
        dataclasses.replace(DEFAULT_OPTIONS, allow_ocr=False),
        dataclasses.replace(DEFAULT_OPTIONS, text_max_chars=17),
        dataclasses.replace(DEFAULT_OPTIONS, low_text_threshold_chars=3),
        dataclasses.replace(DEFAULT_OPTIONS, accepted_mimes=frozenset({"application/pdf"})),
    ],
)
def test_every_option_that_changes_the_answer_changes_the_key(options: Any) -> None:
    digest = hashlib.sha256(DOCUMENT).hexdigest()

    assert conversion_cache_key(digest, options=options) != conversion_cache_key(digest)


def test_installing_a_converter_changes_the_key() -> None:
    """The attachment path's own argument: a machine that gains OCR must re-read."""
    digest = hashlib.sha256(DOCUMENT).hexdigest()

    assert conversion_cache_key(digest, backends=()) != conversion_cache_key(
        digest, backends=default_backends()
    )


def test_a_bumped_normalizer_generation_retires_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reflow runs after the converter, so the contract version cannot see it."""
    from agent_control_knowledge_sync import conversion_cache as module

    digest = hashlib.sha256(DOCUMENT).hexdigest()
    before = conversion_cache_key(digest)
    monkeypatch.setattr(module, "NORMALIZER_VERSION", NORMALIZER_VERSION + 1)

    assert conversion_cache_key(digest) != before


def test_the_cache_hashes_the_bytes_the_module_function_would_have(
    cache: CorpusConversionCache,
) -> None:
    """The backend probe is done once per process; the key must not move for it."""
    assert cache.key_for(DOCUMENT) == conversion_cache_key(hashlib.sha256(DOCUMENT).hexdigest())


# --- Hits, misses, and the work they do or do not do ------------------------


def test_a_second_conversion_of_the_same_bytes_does_no_conversion_work(
    installed: CorpusConversionCache,
    shipped: CountingConverter,
) -> None:
    """The whole point: same bytes, one conversion, and the same markdown twice."""
    first = convert_document(DOCUMENT, declared_mime=OFFICE_MIME)
    assert shipped.calls == 1

    second = convert_document(DOCUMENT, declared_mime=OFFICE_MIME)

    assert shipped.calls == 1
    assert second == first
    assert second.indexable


def test_different_bytes_are_a_different_document(
    installed: CorpusConversionCache,
    shipped: CountingConverter,
) -> None:
    convert_document(DOCUMENT, declared_mime=OFFICE_MIME)
    convert_document(DOCUMENT + b" edited", declared_mime=OFFICE_MIME)

    assert shipped.calls == 2


def test_what_is_stored_is_the_reflowed_markdown_the_chunker_wants(
    corpus: Any,
    installed: CorpusConversionCache,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caching the raw text would leave the reflow to run on every pass."""
    hard_wrapped = "The handbook says laptops are\nordered on the first day of\nthe first week."
    monkeypatch.setattr(
        convert_module,
        "shipped_converter",
        CountingConverter(Converted(text=hard_wrapped, status="text_layer_extracted")),
    )

    converted = convert_document(DOCUMENT, declared_mime=OFFICE_MIME)

    assert "\n" not in converted.text
    assert scalar(corpus, "SELECT body FROM conversion_cache") == converted.text


def test_an_injected_converter_goes_round_the_installed_cache(
    corpus: Any,
    installed: CorpusConversionCache,
    shipped: CountingConverter,
) -> None:
    """The key describes the shipped pipeline, so it cannot answer for another one."""
    other = CountingConverter(Converted(text="something else entirely", status="exported"))

    convert_document(DOCUMENT, declared_mime=OFFICE_MIME, converter=other)
    convert_document(DOCUMENT, declared_mime=OFFICE_MIME, converter=other)

    assert other.calls == 2
    assert shipped.calls == 0
    assert query(corpus, CACHED_ROWS) == []


# --- Failures are failures, not empty successes -----------------------------


def test_a_conversion_failure_comes_back_as_the_failure_it_was(
    corpus: Any,
    installed: CorpusConversionCache,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stored explicitly, status and code intact, so the row it writes is unchanged."""
    failing = CountingConverter(
        Converted(text="", status="failed", error_code="encrypted_document")
    )
    monkeypatch.setattr(convert_module, "shipped_converter", failing)

    first = convert_document(DOCUMENT, declared_mime=OFFICE_MIME)
    second = convert_document(DOCUMENT, declared_mime=OFFICE_MIME)

    assert failing.calls == 1
    assert second == first
    assert not second.indexable
    assert query(corpus, CACHED_ROWS)[0]["error_code"] == "encrypted_document"


def test_a_success_with_no_text_behind_it_is_never_stored(
    corpus: Any,
    installed: CorpusConversionCache,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one shape that must not survive a run: an empty answer wearing a good status."""
    hollow = CountingConverter(Converted(text="   \n ", status="text_layer_extracted"))
    monkeypatch.setattr(convert_module, "shipped_converter", hollow)

    convert_document(DOCUMENT, declared_mime=OFFICE_MIME)
    convert_document(DOCUMENT, declared_mime=OFFICE_MIME)

    assert hollow.calls == 2
    assert query(corpus, CACHED_ROWS) == []


def test_an_empty_success_already_in_the_table_reads_as_a_miss_and_is_dropped(
    corpus: Any,
    installed: CorpusConversionCache,
    shipped: CountingConverter,
) -> None:
    """A row from an older build, or a hand edit, does not get served for thirty days."""
    execute(
        corpus,
        "INSERT INTO conversion_cache (key, status, body, stored_at, last_used_at) "
        "VALUES (:key, 'text_layer_extracted', '', now(), now())",
        key=installed.key_for(DOCUMENT),
    )

    converted = convert_document(DOCUMENT, declared_mime=OFFICE_MIME)

    assert shipped.calls == 1
    assert converted.text == MARKDOWN
    assert query(corpus, CACHED_ROWS)[0]["body"] == MARKDOWN


# --- Never a correctness dependency ----------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        pytest.param(UNREACHABLE, id="refused"),
        pytest.param("postgresql+asyncpg://nobody@127.0.0.1:1/agent_knowledge", id="wrong-driver"),
    ],
)
def test_a_cache_that_cannot_answer_converts_instead_of_failing(
    url: str,
    shipped: CountingConverter,
) -> None:
    """The sweep on the way in is the probe, so a run gets a working cache or none."""
    with open_conversion_cache(url) as store:
        assert store is None

        converted = convert_document(DOCUMENT, declared_mime=OFFICE_MIME)
        convert_document(DOCUMENT, declared_mime=OFFICE_MIME)

    assert converted.text == MARKDOWN
    assert shipped.calls == 2


def test_a_cache_that_fails_once_stops_being_asked() -> None:
    """Twenty thousand refused connections is a worse run than no cache at all."""
    engine = sa.create_engine(UNREACHABLE, future=True, poolclass=NullPool)
    store = CorpusConversionCache(engine)

    assert store.get("ksn1:acv1:whatever") is None
    assert not store.live
    store.put("ksn1:acv1:whatever", Converted(text=MARKDOWN, status="exported"))

    assert store.get("ksn1:acv1:whatever") is None
    store.close()


def test_opening_the_cache_puts_the_previous_one_back(
    corpus: Any,
    cache: CorpusConversionCache,
) -> None:
    previous = install_conversion_cache(cache)
    try:
        with open_conversion_cache(corpus.sync_url) as store:
            assert store is not None
            assert installed_conversion_cache() is store
        assert installed_conversion_cache() is cache
    finally:
        install_conversion_cache(previous)


# --- The bound --------------------------------------------------------------


def test_a_conversion_nothing_has_read_inside_the_window_is_dropped(
    corpus: Any,
    cache: CorpusConversionCache,
) -> None:
    """The eviction rule, whole: age since last read, swept once per run."""
    execute(
        corpus,
        "INSERT INTO conversion_cache (key, status, body, stored_at, last_used_at) VALUES "
        "('stale', 'exported', 'old', now() - interval '400 days', now() - interval '400 days'), "
        "('fresh', 'exported', 'new', now(), now())",
    )

    assert cache.sweep() == 1
    assert [row["key"] for row in query(corpus, CACHED_ROWS)] == ["fresh"]


def test_a_hit_keeps_a_conversion_out_of_the_sweep(
    corpus: Any,
    cache: CorpusConversionCache,
) -> None:
    """A document still in the corpus is read every pass, which is what keeps it."""
    key = cache.key_for(DOCUMENT)
    long_ago = "now() - interval '400 days'"
    execute(
        corpus,
        "INSERT INTO conversion_cache (key, status, body, stored_at, last_used_at) "
        f"VALUES (:key, 'exported', 'old', {long_ago}, {long_ago})",
        key=key,
    )

    assert cache.get(key) is not None
    assert cache.sweep() == 0
    assert [row["key"] for row in query(corpus, CACHED_ROWS)] == [key]


def test_the_reader_role_is_kept_out_of_the_cache(corpus: Any) -> None:
    """Converted text has not been through the deny-list yet; the chunks it becomes have."""
    engine = sa.create_engine(corpus.read_url, future=True, poolclass=NullPool)
    try:
        with pytest.raises(sa.exc.ProgrammingError) as caught, engine.connect() as conn:
            conn.execute(sa.text("SELECT count(*) FROM conversion_cache"))
    finally:
        engine.dispose()

    assert "permission denied" in str(caught.value)
