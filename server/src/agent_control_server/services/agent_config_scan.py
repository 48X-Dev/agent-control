"""Advisory content scan run when a system prompt body is saved.

Non-blocking, on purpose, and the reasoning changed rather than being assumed.
The earlier position was that scanning an ADMIN-authored field is theatre,
because the author sits at the same tier as the control author. That holds only
while the field has exactly one author tier. Source reporting admits
AUTHENTICATED-authored text into the same editor through a human who clicks
"copy into editor", so the field acquires a second, lower-trust author and the
scan starts earning its place.

What it is for is the *record*, including the record that a human saw a finding
and saved anyway. A blocking check on a field admins own produces false
positives that operators route around, which is worse than a note nobody can
delete.

Findings never carry the matched text. A finding on a secret-shaped string that
quoted the string would copy the secret into the version row and into every API
response that reads the history, which is the opposite of the point.

**The rule-pack evaluator is deliberately not wired in here.** The plan named
``DefenseClawRulePackEvaluator`` as the second check. In this tree that
evaluator's ``evaluate`` returns ``no_op_result()`` - it is a registered stub
with no rules - and ``evaluators/contrib`` is excluded from the uv workspace, so
wiring it would mean an optional import and a soft dependency on a package the
server does not install, in exchange for a result that is empty by construction.
The seam is one function call in :func:`scan_prompt_body`; when that evaluator
grows real rules, it goes there.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from agent_control_models.agent_configs import ScanFinding

# Vendor-prefixed credentials. Anchored on the prefix rather than on entropy so
# a short, obviously-shaped key is caught even when it would not clear the
# entropy bar below.
_TOKEN_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "openai_api_key",
        "Looks like an OpenAI-style API key (sk-...).",
        re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    ),
    (
        "github_token",
        "Looks like a GitHub token (ghp_/gho_/ghs_/ghu_/ghr_...).",
        re.compile(r"\bgh[posur]_[A-Za-z0-9]{20,}"),
    ),
    (
        "aws_access_key_id",
        "Looks like an AWS access key id (AKIA.../ASIA...).",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ),
    (
        "google_api_key",
        "Looks like a Google API key (AIza...).",
        re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ),
    (
        "slack_token",
        "Looks like a Slack token (xox...).",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
    ),
    (
        "linear_api_key",
        "Looks like a Linear API key (lin_api_...).",
        re.compile(r"\blin_api_[A-Za-z0-9]{20,}"),
    ),
    (
        "private_key_block",
        "Contains a PEM private key header.",
        re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    ),
    (
        "authorization_header",
        "Contains an Authorization header with a credential.",
        re.compile(r"(?im)^\s*authorization\s*:\s*(?:bearer|basic)\s+\S+"),
    ),
    (
        "bearer_token",
        "Contains a bearer token.",
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}"),
    ),
)

# A standalone run of credential-ish characters, long enough to be a secret and
# not a word. Deliberately requires a mix of cases or digits, because a long
# lowercase run is prose or a URL slug.
_HIGH_ENTROPY_CANDIDATE = re.compile(r"\b[A-Za-z0-9+/=_\-]{32,}\b")
_HIGH_ENTROPY_BITS_PER_CHAR = 3.6

_MAX_FINDINGS = 20


def _shannon_bits_per_char(value: str) -> float:
    counts = Counter(value)
    length = len(value)
    return -sum((n / length) * math.log2(n / length) for n in counts.values())


def _looks_like_a_secret(candidate: str) -> bool:
    """Whether a long token is random enough to be worth flagging.

    Two gates, and both are needed. Entropy alone flags base64-ish prose and
    long hyphenated identifiers; a character-class mix alone flags any long
    CamelCase phrase. Together they miss some real secrets and that is the right
    trade for an advisory check whose false positives an operator has to read
    every time they save.
    """
    has_digit = any(c.isdigit() for c in candidate)
    has_upper = any(c.isupper() for c in candidate)
    has_lower = any(c.islower() for c in candidate)
    mixed = has_digit and (has_upper or has_lower) or (has_upper and has_lower)
    if not mixed:
        return False
    return _shannon_bits_per_char(candidate) >= _HIGH_ENTROPY_BITS_PER_CHAR


def scan_prompt_body(body: str | None) -> list[ScanFinding]:
    """Return advisory findings for a body about to be saved.

    Never raises and never rejects. A scanner that can fail a save is a scanner
    that can take the editor down.
    """
    if not body:
        return []

    findings: list[ScanFinding] = []

    for code, message, pattern in _TOKEN_PATTERNS:
        matches = pattern.findall(body)
        if matches:
            findings.append(
                ScanFinding(
                    scanner="secret_pattern",
                    severity="warning",
                    code=code,
                    message=(
                        f"{message} A system prompt is readable by any key in "
                        "this namespace, and its history survives clearing."
                    ),
                    match_count=len(matches),
                )
            )

    entropy_hits = sum(
        1 for candidate in _HIGH_ENTROPY_CANDIDATE.findall(body) if _looks_like_a_secret(candidate)
    )
    if entropy_hits:
        findings.append(
            ScanFinding(
                scanner="secret_pattern",
                severity="warning",
                code="high_entropy_string",
                message=(
                    "Contains one or more long, random-looking strings. If any "
                    "of them is a credential, note that a system prompt is "
                    "readable by any key in this namespace and its history "
                    "survives clearing."
                ),
                match_count=entropy_hits,
            )
        )

    return findings[:_MAX_FINDINGS]
