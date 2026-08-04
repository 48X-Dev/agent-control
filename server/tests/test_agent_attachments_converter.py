"""Section 11's converter suite: what the library refuses to do, proved by absence.

``test_attachment_converter.py`` proves the pipeline reaches the right answer.
This file proves the things a passing answer cannot show, which is most of what
the plan actually promises about conversion:

- the library is **pure** - no configuration, no database, no socket, no
  authorization surface at all - so the answer cannot vary with the deployment
  it runs in, and cannot vary with which auth provider is installed
- a document's own bytes and a parser's own words reach **no field** of the
  result on any failure path, following the E2/H2 proof-by-absence pattern this
  branch already uses at the Exa wire and the ADK contract
- the cache key folds in **every** option, including one added tomorrow, and
  survives a process restart
- the accepted MIME set, the sniffer and the converter's extension table
  describe the same four types rather than three overlapping guesses
- nothing in the result invites a caller to believe a page cap is enforced,
  because section 9 says plainly that it is not

Every backend here is a fake. The two real libraries are covered in
``test_attachment_converter_backends.py``, which skips when they are absent.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import inspect
import io
import os
import subprocess
import sys
import zipfile
from dataclasses import fields, replace
from pathlib import Path

import pytest
from agent_control_models.files import sniff_mime

from agent_control_server.services import attachment_converter as public
from agent_control_server.services import attachment_converter_backends as backend_module
from agent_control_server.services.attachment_converter import (
    DEFAULT_CONVERTIBLE_MIMES,
    LOW_TEXT_THRESHOLD_CHARS,
    AttemptOutcome,
    ConversionOptions,
    ConversionResult,
    ConversionStatus,
    ConverterAttempt,
    content_sha256,
    convert_attachment,
    convert_attachment_async,
)
from agent_control_server.services.attachment_converter_backends import (
    _EXTENSION_BY_MIME,
    ConverterFailedError,
    ConverterKind,
    DoclingBackend,
    MarkItDownBackend,
    _module_installed,
)
from agent_control_server.services.attachment_converter_cache import (
    CONVERSION_CONTRACT_VERSION,
    conversion_cache_key,
)
from agent_control_server.services.attachment_converter_containers import (
    OOXML_DOCUMENT,
    OOXML_PRESENTATION,
    OOXML_SHEET,
    refine_container_mime,
)

PDF = b"%PDF-1.7\n" + b"trailer\n" * 8
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
ZIP = b"PK\x03\x04" + b"\x00" * 64


def _ooxml(root: str) -> bytes:
    """A minimal real OOXML container.

    Built rather than pasted because the property under test is structural: a
    ZIP carrying ``[Content_Types].xml`` and one well-known root directory. A
    hand-written byte string would pass whatever the refiner happened to do.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(f"{root}/document.xml", "<x/>")
    return buffer.getvalue()


PPTX = _ooxml("ppt")
DOCX = _ooxml("word")
XLSX = _ooxml("xl")
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64
POSTSCRIPT = b"%!PS-Adobe-3.0\n" + b"\x00" * 64

BODY = "Slide one, revenue up, churn flat. " * 20
OCR_BODY = "Recovered headline text. " * 22

CONVERTER_SOURCES = (
    "attachment_converter.py",
    "attachment_converter_types.py",
    "attachment_converter_backends.py",
    "attachment_converter_cache.py",
    "attachment_converter_containers.py",
)

BANNED_IMPORT_ROOTS = frozenset(
    {
        "aiohttp",
        "boto3",
        "fastapi",
        "httpx",
        "psycopg",
        "requests",
        "socket",
        "sqlalchemy",
        "ssl",
        "starlette",
        "urllib",
    }
)
BANNED_SERVER_MODULES = frozenset(
    {
        "agent_control_server.auth_framework",
        "agent_control_server.config",
        "agent_control_server.db",
        "agent_control_server.main",
        "agent_control_server.models",
    }
)


class FakeBackend:
    """A converter that does what the test told it to, and records the bytes."""

    def __init__(
        self,
        name: str,
        kind: ConverterKind,
        *,
        text: str | None = "",
        raises: BaseException | None = None,
        available: bool = True,
    ) -> None:
        self.name = name
        self.kind = kind
        self._text = text
        self._raises = raises
        self._available = available
        self.calls = 0
        self.seen: list[bytes] = []
        self.mimes: list[str] = []

    def available(self) -> bool:
        return self._available

    def extract(self, data: bytes, *, mime: str) -> str:
        self.calls += 1
        self.seen.append(data)
        self.mimes.append(mime)
        if self._raises is not None:
            raise self._raises
        return self._text  # type: ignore[return-value]


def text_layer(**kwargs: object) -> FakeBackend:
    return FakeBackend("markitdown", ConverterKind.TEXT_LAYER, **kwargs)  # type: ignore[arg-type]


def ocr(**kwargs: object) -> FakeBackend:
    return FakeBackend("docling", ConverterKind.OCR, **kwargs)  # type: ignore[arg-type]


def converter_source(name: str) -> Path:
    return Path(public.__file__).parent / name


def stable(result: ConversionResult) -> ConversionResult:
    """The result with wall-clock durations flattened, so two runs can be equal."""
    return replace(
        result,
        attempts=tuple(replace(a, duration_seconds=0.0) for a in result.attempts),
    )


class _Exploding:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"the converter read configuration: {name}")


class TestPurity:
    """The library reads nothing about the deployment, so it cannot vary with it.

    This is the slice's whole contract with slice 3: bytes in, text out, and the
    storing, scheduling and authorizing all happen somewhere that can see a
    request. Asserted on the source rather than on behaviour, because a module
    that imports settings and happens not to read them today is one edit away
    from a converter whose answer depends on which container it runs in.
    """

    @pytest.mark.parametrize("source", CONVERTER_SOURCES)
    def test_no_converter_module_imports_settings_a_database_or_a_socket(self, source: str) -> None:
        tree = ast.parse(converter_source(source).read_text(encoding="utf-8"))

        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module)

        roots = {name.split(".")[0] for name in imported}
        assert not roots & BANNED_IMPORT_ROOTS
        assert not {
            name for name in imported if any(name.startswith(b) for b in BANNED_SERVER_MODULES)
        }

    @pytest.mark.parametrize("source", CONVERTER_SOURCES)
    def test_every_relative_import_stays_inside_the_converter(self, source: str) -> None:
        """A sibling service is a database call one refactor away."""
        tree = ast.parse(converter_source(source).read_text(encoding="utf-8"))

        siblings = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level > 0 and node.module
        }

        assert all(name.startswith("attachment_converter") for name in siblings)

    def test_a_conversion_asks_for_no_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Runtime proof to go with the source scan.

        The scan covers imports written today. This covers the one an inline
        ``from ..config import`` inside a function would add, by making every
        attribute of the live settings objects raise for the length of a
        conversion and a cache-key computation.
        """
        from agent_control_server import config

        for name in ("auth_settings", "executor_settings", "linear_settings"):
            if hasattr(config, name):
                monkeypatch.setattr(config, name, _Exploding())

        result = convert_attachment(PDF, backends=(text_layer(text=BODY),))
        key = conversion_cache_key(result.source_sha256)

        assert result.status is ConversionStatus.TEXT_LAYER_EXTRACTED
        assert key

    def test_the_answer_is_identical_under_both_auth_providers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both providers, and the honest form of that requirement for a library.

        ``api_key_enabled`` false installs ``NoAuthProvider``, which authorizes
        every operation and leaves ``caller_id`` None; true installs a provider
        that does not. That difference decides who may upload these bytes and it
        is slice 3's to enforce. Here it must decide **nothing**, and the way to
        show that is to flip it and get the same object back.
        """
        from agent_control_server import config

        answers = []
        for enabled in (False, True):
            monkeypatch.setattr(config.auth_settings, "api_key_enabled", enabled)
            answers.append(
                stable(convert_attachment(PNG, backends=(text_layer(text=""), ocr(text=OCR_BODY))))
            )

        assert answers[0] == answers[1]

    def test_no_entry_point_accepts_a_caller_a_namespace_or_a_session(self) -> None:
        """Proof by absence: there is no authorization input to get wrong.

        A converter that took a principal would eventually decide something with
        it, and that decision would be a second authorization rule beside
        ``require_content_access``. There is no parameter, so there is nothing
        to diverge from.
        """
        for entry in (convert_attachment, convert_attachment_async, conversion_cache_key):
            names = set(inspect.signature(entry).parameters)
            assert not names & {
                "principal",
                "caller_id",
                "caller_hash",
                "namespace_key",
                "session_id",
                "settings",
                "db",
                "session",
            }


class TestNothingLeaksOutOfAFailure:
    """A parser's words and a document's bytes reach no field of the result.

    The plan's rule for the sidecar client is "no upstream bytes in any response";
    the same rule applies to a library whose failure is going to be written to an
    operator console and an attachment row. Asserted over the whole result rather
    than over ``text``, because a code, a converter name or an exception repr is
    just as good a channel.
    """

    def test_a_parser_error_carrying_document_text_reaches_no_field(self) -> None:
        secret = "Acme Corp owes 412,000 as of March"

        class Leaky(Exception):
            pass

        result = convert_attachment(
            PDF,
            backends=(
                text_layer(raises=Leaky(secret)),
                ocr(raises=ConverterFailedError("converter_error")),
            ),
        )

        assert result.status is ConversionStatus.FAILED
        assert secret not in repr(result)
        assert all(secret not in repr(a) for a in result.attempts)
        assert result.text == ""

    def test_a_failure_code_is_always_one_of_the_hand_written_constants(self) -> None:
        known = {
            value
            for module in (public, backend_module)
            for name, value in vars(module).items()
            if name.startswith("FAILURE_") and isinstance(value, str)
        }

        result = convert_attachment(
            PDF, backends=(text_layer(raises=ValueError("page 3: Acme Corp")),)
        )

        assert result.attempts[0].failure_code in known
        assert result.failure_code in known

    def test_an_attempt_cannot_carry_text_at_all(self) -> None:
        """Proof by absence: the losing pass has nowhere to keep the document.

        ``_Pass`` holds the text while the pipeline decides and
        :class:`ConverterAttempt` is what escapes. Only the winner's text leaves
        this library, so an echoed or rejected extraction has no field to ride
        out on even if a later edit forgets to clear one.
        """
        names = {f.name for f in fields(ConverterAttempt)}

        assert "text" not in names
        assert names == {
            "name",
            "kind",
            "outcome",
            "text_chars",
            "meaningful_chars",
            "duration_seconds",
            "failure_code",
        }

    def test_an_echoed_source_survives_nowhere_in_the_result(self) -> None:
        """MarkItDown's measured echo, asserted over every field rather than ``text``.

        The corrupt body is 156 meaningful characters of the file's own bytes,
        which clears every threshold there is. A status and a character count
        both look healthy, so the only assertion worth making is that the bytes
        are gone.
        """
        marker = "this is not a document at all"
        corrupt = b"%PDF-1.4\n" + (marker + ". ").encode() * 6

        result = convert_attachment(corrupt, backends=(text_layer(text=corrupt.decode()),))

        assert result.status is ConversionStatus.FAILED
        assert result.failure_code == "source_echoed"
        assert marker not in repr(result)
        assert result.meaningful_chars == 0

    def test_the_echo_guard_also_covers_the_ocr_pass(self) -> None:
        """An escalation that echoes is a failed escalation, not a recovered image."""
        result = convert_attachment(
            PNG, backends=(text_layer(text=""), ocr(text=PNG.decode("latin-1")))
        )

        assert result.attempts[1].outcome is AttemptOutcome.FAILED
        assert result.status is ConversionStatus.FAILED
        assert result.text == ""

    def test_an_empty_extraction_is_not_mistaken_for_an_echo(self) -> None:
        """The guard compares against the input's first bytes, and "" matches none.

        Worth pinning: if it did match, every unreadable PNG - five of the six
        real attachments - would be reported ``failed`` instead of escalating.
        """
        md, dl = text_layer(text=""), ocr(text=OCR_BODY)

        result = convert_attachment(PNG, backends=(md, dl))

        assert md.calls == 1
        assert result.attempts[0].outcome is AttemptOutcome.LOW_TEXT
        assert result.status is ConversionStatus.OCR_EXTRACTED

    def test_text_that_merely_mentions_the_magic_number_is_kept(self) -> None:
        """The guard is deliberately narrow: only a prefix match is an echo."""
        body = f"The specification says every file begins with %PDF-1.7. {BODY}"

        result = convert_attachment(PDF, backends=(text_layer(text=body),))

        assert result.status is ConversionStatus.TEXT_LAYER_EXTRACTED
        assert result.text == body


class TestTheTypeGateAndTheSniffAgree:
    """One accepted set, one sniffer, one extension table, or a dead entry."""

    def test_every_convertible_type_can_be_sniffed_and_routed(self) -> None:
        fixtures = {
            "application/pdf": PDF,
            "image/png": PNG,
            "image/jpeg": JPEG,
            "image/webp": WEBP,
            # These three sniff as application/zip and are only reachable
            # through the structural refinement, which is exactly why they
            # belong in this test: a convertible type nothing can produce is
            # the dead entry this class exists to catch.
            OOXML_PRESENTATION: PPTX,
            OOXML_DOCUMENT: DOCX,
            OOXML_SHEET: XLSX,
        }

        assert set(fixtures) == set(DEFAULT_CONVERTIBLE_MIMES)
        assert all(
            refine_container_mime(data, sniff_mime(data)) == mime
            for mime, data in fixtures.items()
        )
        assert set(DEFAULT_CONVERTIBLE_MIMES) <= set(_EXTENSION_BY_MIME)

    @pytest.mark.parametrize(
        ("data", "sniffed"),
        [
            (ZIP, "application/zip"),
            (OLE, "application/x-ole-storage"),
            (GIF, "image/gif"),
            (POSTSCRIPT, "application/postscript"),
        ],
    )
    def test_a_sniffable_type_outside_the_set_builds_no_parser(
        self, data: bytes, sniffed: str
    ) -> None:
        """3.4's refusal, at the library level: a .pptx is a ZIP and stays refused."""
        md, dl = text_layer(text=BODY), ocr(text=OCR_BODY)

        result = convert_attachment(data, declared_mime="application/pdf", backends=(md, dl))

        assert result.status is ConversionStatus.UNSUPPORTED_TYPE
        assert result.sniffed_mime == sniffed
        assert (md.calls, dl.calls) == (0, 0)

    @pytest.mark.parametrize(
        ("data", "mime"),
        [(PDF, "application/pdf"), (PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp")],
    )
    def test_the_backend_is_told_the_sniffed_type_for_every_accepted_kind(
        self, data: bytes, mime: str
    ) -> None:
        md = text_layer(text=BODY)

        convert_attachment(data, declared_mime="application/octet-stream", backends=(md,))

        assert md.mimes == [mime]

    def test_a_refused_file_still_carries_its_hash(self) -> None:
        """So a caller can cache the refusal instead of re-sniffing it forever."""
        refused = convert_attachment(ZIP, backends=(text_layer(),))
        empty = convert_attachment(b"", backends=(text_layer(),))

        assert refused.source_sha256 == content_sha256(ZIP)
        assert empty.source_sha256 == content_sha256(b"")


class TestBackendsAreDataNotHardCodedNames:
    """The pipeline runs the backends it was handed, in kind order, all of them."""

    def test_a_second_text_layer_pass_runs_when_the_first_reads_nothing(self) -> None:
        first, second = (
            text_layer(text=""),
            FakeBackend("secondary", ConverterKind.TEXT_LAYER, text=BODY),
        )
        dl = ocr(text=OCR_BODY)

        result = convert_attachment(PDF, backends=(first, second, dl))

        assert (first.calls, second.calls, dl.calls) == (1, 1, 0)
        assert result.converter == "secondary"
        assert result.status is ConversionStatus.TEXT_LAYER_EXTRACTED
        assert result.escalated is False

    def test_a_second_ocr_pass_runs_when_the_first_breaks(self) -> None:
        broken = FakeBackend("docling", ConverterKind.OCR, raises=ConverterFailedError())
        spare = FakeBackend("spare-ocr", ConverterKind.OCR, text=OCR_BODY)

        result = convert_attachment(PNG, backends=(text_layer(text=""), broken, spare))

        assert result.status is ConversionStatus.OCR_EXTRACTED
        assert result.converter == "spare-ocr"
        assert [a.outcome for a in result.attempts] == [
            AttemptOutcome.LOW_TEXT,
            AttemptOutcome.FAILED,
            AttemptOutcome.EXTRACTED,
        ]

    def test_no_backends_at_all_is_unavailable_and_never_empty(self) -> None:
        """``empty`` claims every converter ran. With none installed, none did."""
        result = convert_attachment(PDF, backends=())

        assert result.status is ConversionStatus.CONVERTER_UNAVAILABLE
        assert result.attempts == ()
        assert result.has_text is False

    def test_the_backend_receives_the_whole_document_unaltered(self) -> None:
        md = text_layer(text=BODY)

        convert_attachment(PDF, backends=(md,))

        assert md.seen == [PDF]
        assert md.seen[0] is PDF

    def test_a_backend_returning_none_is_read_as_no_text(self) -> None:
        result = convert_attachment(PNG, backends=(text_layer(text=None), ocr(text=None)))

        assert result.status is ConversionStatus.EMPTY
        assert result.text == ""

    def test_a_memory_error_inside_a_converter_is_a_status(self) -> None:
        """Docling loads a layout model. The failure mode is a status, not a 500."""
        result = convert_attachment(PNG, backends=(text_layer(text=""), ocr(raises=MemoryError())))

        assert result.status is ConversionStatus.FAILED
        assert result.failure_code == "converter_error"

    @pytest.mark.parametrize("error", [ImportError("no parent"), ValueError("__spec__ is None")])
    def test_the_shipped_adapters_report_unavailable_rather_than_raising(
        self, error: Exception, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``available()`` runs before the try block, so it must not raise."""

        def boom(name: str) -> None:
            raise error

        monkeypatch.setattr(importlib.util, "find_spec", boom)

        assert _module_installed("markitdown") is False
        assert MarkItDownBackend().available() is False
        assert DoclingBackend().available() is False


class TestWhatTheStatusPromises:
    def test_recovered_ocr_text_is_reported_under_a_missing_text_layer_extra(self) -> None:
        """The remedy names the status; the recovered read still travels intact.

        A deployment with the OCR extra and not the text-layer one is a real
        configuration: OCR is the main path for five of this workspace's six
        attachments. Docling recovers the whole image here - fifteen meaningful
        characters, every word in it - while a converter this document would
        have tried is missing, and the status names the absence because "install
        the missing extra" is the sentence an operator needs and the next file
        hits the same hole.

        What that ordering must not do is deny the read. It used to: ``has_text``
        was derived from the status, so this row reported false while the
        headline sat in ``text``, which is the inversion the escalation
        threshold was changed to stop making. The status says how the text was
        got and how far to trust it; ``has_text`` says whether there is any.
        """
        result = convert_attachment(
            PNG, backends=(text_layer(available=False), ocr(text="BOARD REVIEW 2026"))
        )

        assert result.status is ConversionStatus.CONVERTER_UNAVAILABLE
        assert result.text == "BOARD REVIEW 2026"
        assert result.converter == "docling"
        assert result.meaningful_chars == 15
        assert result.has_text is True

    def test_escalated_says_ocr_ran_and_not_merely_that_it_was_configured(self) -> None:
        """Otherwise a cost or latency signal read off this flag overcounts.

        An OCR backend that reports itself unavailable is never handed the
        document. It costs nothing and it recovers nothing, and a result
        flagged escalated would put it in the same bucket as the twenty seconds
        a real Docling pass spends.
        """
        dl = ocr(available=False)

        result = convert_attachment(PNG, backends=(text_layer(text=""), dl))

        assert dl.calls == 0
        assert result.escalated is False
        assert result.attempts[1].outcome is AttemptOutcome.UNAVAILABLE

    def test_escalated_is_true_once_an_ocr_pass_is_actually_entered(self) -> None:
        """Including the one that ran and broke, which is where the cost went."""
        broke = ocr(raises=ConverterFailedError())

        result = convert_attachment(PNG, backends=(text_layer(text=""), broke))

        assert broke.calls == 1
        assert result.escalated is True

    def test_no_status_claims_a_clean_read(self) -> None:
        values = {s.value for s in ConversionStatus}

        assert not values & {"ok", "success", "converted", "complete", "clean"}

    def test_the_result_offers_no_page_count_to_believe(self) -> None:
        """Section 9: the 1,000-page cap is not enforceable without a parser.

        The sidecar sketch in section 8 returned ``page_count`` and per-page
        counters. This library returns neither, and the absence is the honest
        part: a field here would be a number a caller could cap against, and
        nothing in this module can count pages.
        """
        names = {f.name for f in fields(ConversionResult)} | set(vars(ConversionResult))

        assert not [name for name in names if "page" in name.lower()]

    def test_an_encrypted_document_reports_no_text_and_no_converter(self) -> None:
        result = convert_attachment(
            PDF,
            backends=(
                text_layer(raises=backend_module.EncryptedDocumentError()),
                ocr(text=OCR_BODY),
            ),
        )

        assert (result.text, result.converter, result.has_text) == ("", None, False)


class TestTheTextCapIsACapAndNotAPolicy:
    def test_the_boundary_is_exact(self) -> None:
        options = ConversionOptions(text_max_chars=50)

        at = convert_attachment(PDF, options=options, backends=(text_layer(text="y" * 50),))
        over = convert_attachment(PDF, options=options, backends=(text_layer(text="y" * 51),))

        assert (at.text_chars, at.text_truncated) == (50, False)
        assert (over.text_chars, over.text_truncated) == (50, True)

    def test_the_reported_count_belongs_to_the_text_the_caller_receives(self) -> None:
        """The floor and the string it is applied to have to be the same string.

        The module hands the delivery floor to the caller and points it at
        ``meaningful_chars``. That number used to describe the converter's
        output rather than the delivered text, so a floor written the obvious
        way measured five hundred characters the caller did not have. The
        per-converter counts still describe what each parser produced.
        """
        options = ConversionOptions(text_max_chars=50)

        result = convert_attachment(PDF, options=options, backends=(text_layer(text="y" * 500),))

        assert result.meaningful_chars == 50
        assert result.text_chars == 50
        assert result.text_truncated is True
        assert result.attempts[0].meaningful_chars == 500

    def test_a_cap_that_leaves_nothing_readable_is_not_reported_as_text(self) -> None:
        """``has_text`` is counted on the delivered string, so a cut can empty it."""
        options = ConversionOptions(text_max_chars=3)

        result = convert_attachment(PDF, options=options, backends=(text_layer(text="   " + BODY),))

        assert result.text == "   "
        assert result.meaningful_chars == 0
        assert result.has_text is False

    def test_a_result_with_no_text_is_never_flagged_truncated(self) -> None:
        options = ConversionOptions(text_max_chars=1)

        result = convert_attachment(PNG, options=options, backends=(text_layer(text=""), ocr()))

        assert result.text_truncated is False


class TestTheCacheKeyIsCompleteAndStable:
    ALTERNATIVES: dict[str, object] = {
        "accepted_mimes": frozenset({"application/pdf"}),
        "low_text_threshold_chars": LOW_TEXT_THRESHOLD_CHARS + 1,
        "text_max_chars": 4096,
        "allow_ocr": False,
    }

    def test_every_option_field_changes_the_key(self) -> None:
        """Including the field somebody adds next month.

        A new option that does not reach the key means every deployment that
        changes it goes on serving answers computed under the old one, with no
        symptom until an agent reads a document nobody converted that way.
        """
        backends = (text_layer(), ocr())
        digest = content_sha256(PNG)
        baseline = conversion_cache_key(digest, backends=backends)

        assert {f.name for f in fields(ConversionOptions)} == set(self.ALTERNATIVES)
        for name, value in self.ALTERNATIVES.items():
            options = replace(ConversionOptions(), **{name: value})  # type: ignore[arg-type]
            assert conversion_cache_key(digest, options=options, backends=backends) != baseline

    def test_the_contract_version_retires_every_entry_at_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_control_server.services import attachment_converter_cache as cache

        backends = (text_layer(), ocr())
        before = conversion_cache_key(content_sha256(PDF), backends=backends)
        monkeypatch.setattr(cache, "CONVERSION_CONTRACT_VERSION", CONVERSION_CONTRACT_VERSION + 1)
        after = conversion_cache_key(content_sha256(PDF), backends=backends)

        assert before != after
        assert after.startswith(f"acv{CONVERSION_CONTRACT_VERSION + 1}:")

    def test_the_key_takes_no_filename_and_no_declared_type(self) -> None:
        """Proof by absence: two names for one document cannot split the cache."""
        assert set(inspect.signature(conversion_cache_key).parameters) == {
            "source_sha256",
            "options",
            "backends",
        }

    def test_the_key_survives_a_process_restart(self) -> None:
        """Two interpreters, two hash seeds, one key.

        The cache outlives the process that filled it. A key built from
        ``hash()`` of anything would be stable within a run and different after
        a restart, which reads as a permanently cold cache and re-OCRs the
        corpus at twenty seconds a file. Two subprocesses is the only way to
        catch that, so it is worth the second and a half.
        """
        script = (
            "from agent_control_server.services.attachment_converter_cache import "
            "conversion_cache_key; print(conversion_cache_key('a' * 64))"
        )
        keys = []
        for seed in ("0", "1"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            done = subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=120
            )
            assert done.returncode == 0, done.stderr
            keys.append(done.stdout.strip())

        assert keys[0] == keys[1]
        assert keys[0].startswith(f"acv{CONVERSION_CONTRACT_VERSION}:")


class TestTheSameBytesGiveTheSameAnswer:
    def test_two_runs_of_one_document_agree_in_every_field(self) -> None:
        def run() -> ConversionResult:
            return convert_attachment(
                PNG, declared_mime="image/png", backends=(text_layer(text=""), ocr(text=OCR_BODY))
            )

        assert stable(run()) == stable(run())

    def test_the_pipeline_keeps_no_state_between_documents(self) -> None:
        backends = (text_layer(text=""), ocr(text=OCR_BODY))

        first = convert_attachment(PNG, backends=backends)
        refused = convert_attachment(ZIP, backends=backends)
        again = convert_attachment(PNG, backends=backends)

        assert refused.status is ConversionStatus.UNSUPPORTED_TYPE
        assert stable(first) == stable(again)

    async def test_two_conversions_at_once_do_not_borrow_each_other_answers(self) -> None:
        pdf = convert_attachment_async(PDF, backends=(text_layer(text=BODY),))
        png = convert_attachment_async(PNG, backends=(text_layer(text=""), ocr(text=OCR_BODY)))

        first, second = await asyncio.gather(pdf, png)

        assert (first.status, first.text) == (ConversionStatus.TEXT_LAYER_EXTRACTED, BODY)
        assert (second.status, second.text) == (ConversionStatus.OCR_EXTRACTED, OCR_BODY)

    async def test_a_broken_converter_does_not_raise_through_the_async_path(self) -> None:
        result = await convert_attachment_async(
            PDF, backends=(text_layer(raises=RuntimeError("boom")),)
        )

        assert result.status is ConversionStatus.FAILED
        assert result.failure_code == "converter_error"
