"""Editable presentation style ("persona"): the built-in default or an optional Google Doc.

Two parts with one job each:
- GoogleDocReader fetches one public Google Doc as bounded plain text.
- PersonaStyle keeps the style used for the current question and keeps the last
  good version (or the built-in default) when a refresh fails.
The style only shapes presentation; the mandatory policy is analyst_policy.md.
"""

import logging
import re
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)
DEFAULT_STYLE = files("retail_agent.llm").joinpath("prompts/default_style.md").read_text(encoding="utf-8").strip()


@dataclass(frozen=True)
class Persona:
    text: str = DEFAULT_STYLE
    source: str = "default"

    @property
    def version(self) -> str:
        return sha256(self.text.encode("utf-8")).hexdigest()[:12]


def parse_google_doc_id(value: str) -> str:
    """A bare document ID, or the ID from a standard https://docs.google.com/document/... URL."""
    value = value.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{20,160}", value):
        return value
    url = urlsplit(value)
    if url.scheme == "https" and url.netloc == "docs.google.com":
        match = re.fullmatch(r"/document/(?:u/\d+/)?d/([A-Za-z0-9_-]{20,160})(?:/.*)?", url.path)
        if match:
            return match.group(1)
    raise ValueError("Expected a document ID or a standard HTTPS Google Docs URL")


class DocumentReadError(RuntimeError):
    """Safe failure: a stable code, the cause's class name and HTTP status; never its text or URL."""

    def __init__(self, code: str, cause: Exception):
        super().__init__(code)
        self.code = code
        self.error_type = type(cause).__name__
        self.http_status = cause.code if isinstance(cause, HTTPError) else None


class GoogleDocReader:
    """Reads a Google Doc shared as "Anyone with the link: Viewer" through its text export."""

    timeout_seconds = 3
    max_bytes = 16_000
    max_chars = 4000

    def __init__(self, document: str):
        self.document = document  # ID or URL from configuration.

    def read(self) -> str:
        try:
            doc_id = parse_google_doc_id(self.document)
        except Exception as error:
            raise DocumentReadError("persona_invalid_config", error) from None
        # Rebuild the export endpoint from the ID instead of fetching a configured URL.
        request = Request(
            f"https://docs.google.com/document/d/{doc_id}/export?format=txt", headers={"Accept": "text/plain"}
        )
        try:
            response = urlopen(request, timeout=self.timeout_seconds)
        except Exception as error:
            raise DocumentReadError("persona_fetch_failed", error) from None
        try:
            with response:
                return self._bounded_text(response)
        except Exception as error:
            raise DocumentReadError("persona_invalid_content", error) from None

    def _bounded_text(self, response) -> str:
        if response.headers.get_content_type() != "text/plain":
            raise ValueError("Expected plain text, not a login/error page")
        content = response.read(self.max_bytes + 1)
        if len(content) > self.max_bytes:
            raise ValueError("Document too large")
        text = content.decode("utf-8-sig").strip()
        if not text or len(text) > self.max_chars:
            raise ValueError("Empty or oversized persona")
        return text


class PersonaStyle:
    """The current style; without a reader it is always the built-in default."""

    def __init__(self, reader: GoogleDocReader | None = None):
        self.reader = reader
        self.current = Persona()

    def refresh(self) -> Persona:
        """Re-read the document; on failure keep the last good version."""
        if self.reader is None:
            return self.current
        try:
            text = self.reader.read()
        except DocumentReadError as error:
            # No URLs, contents or raw HTTP exceptions in local logs.
            logger.warning(
                "persona_load_failed",
                extra={
                    "error_code": error.code,
                    "persona_version": self.current.version,
                    "error_type": error.error_type,
                    **({"http_status": error.http_status} if error.http_status is not None else {}),
                },
            )
            return self.current
        self.current = Persona(text, "google_doc")
        logger.info("persona_loaded", extra={"persona_version": self.current.version, "status": "ok"})
        return self.current
