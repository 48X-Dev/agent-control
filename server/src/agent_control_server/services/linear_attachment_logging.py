"""Keeping upload URLs out of the log, including out of httpx's own line.

Plan section 3.9 says the URL is never logged at any level, and
:mod:`services.linear_attachments` keeps that rule in every line it writes
itself. It is not the only thing writing lines. ``httpx`` logs
``HTTP Request: GET <full url> "HTTP/1.1 200 OK"`` at INFO from a module-level
logger, and this deployment's root level is INFO by default
(``logging_utils.configure_logging``), so without this filter every attachment
URL a step fetched would be sitting in the deployment's log.

A filter rather than a level change, because silencing ``httpx`` would also
silence every other client in the process, and an operator debugging an
executor call would lose the line that tells them the request was made at all.
This keeps the line and takes the path out of it.
"""

from __future__ import annotations

import logging
import re

HTTPX_LOGGER_NAME = "httpx"

REDACTED = "[redacted]"


class UploadUrlRedaction(logging.Filter):
    """Rewrites any allowlisted upload URL in a record down to its host.

    Never drops a record. A filter that suppressed the line would hide that a
    request happened, which is the opposite of what a log is for; what is
    removed is the path, which is the part that names somebody's file.
    """

    def __init__(self, hosts: set[str]) -> None:
        super().__init__()
        self.hosts: set[str] = {host.lower() for host in hosts}
        self._pattern = self._compile()

    def _compile(self) -> re.Pattern[str] | None:
        if not self.hosts:
            return None
        alternatives = "|".join(re.escape(host) for host in sorted(self.hosts))
        return re.compile(rf"(https?://(?:{alternatives}))/\S*", re.IGNORECASE)

    def add(self, hosts: set[str]) -> None:
        before = len(self.hosts)
        self.hosts |= {host.lower() for host in hosts}
        if len(self.hosts) != before:
            self._pattern = self._compile()

    def filter(self, record: logging.LogRecord) -> bool:
        if self._pattern is None:
            return True
        message = record.getMessage()
        redacted = self._pattern.sub(rf"\1/{REDACTED}", message)
        if redacted != message:
            # Collapsed to a rendered string, because the URL may live in any
            # of the args and rewriting one of them in place would depend on
            # the position httpx happens to use.
            record.msg = redacted
            record.args = ()
        return True


def redact_upload_urls(hosts: set[str]) -> None:
    """Install the filter on the ``httpx`` logger, or widen the one already on it.

    Idempotent, because it is called from every client this process builds and
    a filter per step would be a slow leak on a long-running server.
    """
    logger = logging.getLogger(HTTPX_LOGGER_NAME)
    for existing in logger.filters:
        if isinstance(existing, UploadUrlRedaction):
            existing.add(hosts)
            return
    logger.addFilter(UploadUrlRedaction(hosts))
