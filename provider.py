"""Machine Library web search + extract provider for Hermes Agent.

Search covers papers, books, patents, standards and Wikipedia
(``POST /v2/search/``, ``documents`` index). Extract reads Machine Library
document pages (``https://machinelibrary.ai/r/...``) and source links Machine
Library resolves to a stored document (DOI, arXiv, ISBN, standard identifiers)
via ``GET /v2/documents/by-uri/{uri}``. Other URLs fail per URL, so pair
another extract backend for the open web.

Env: ``MACHINELIBRARY_API_KEY`` (required; create one at
https://machinelibrary.ai/keys). Standard library HTTP only.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from agent.secret_scope import get_secret
from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

__version__ = "0.1.0"

API_URL = "https://api.machinelibrary.ai"
SITE_URL = "https://machinelibrary.ai"
KEY_ENV = "MACHINELIBRARY_API_KEY"

MAX_SEARCH_LIMIT = 20
SNIPPET_CHARS = 600
# Hermes truncates extracted pages to web.extract_char_limit (15,000 by
# default) after the call; fetching a little more keeps its head+tail cut
# meaningful while billing stays bounded.
EXTRACT_MAX_CHARS = 20_000
SEARCH_TIMEOUT = 60
FETCH_TIMEOUT = 60


class MachineLibraryError(Exception):
    """A request failed; the message is safe to show the model."""


def _api_key() -> str:
    return (get_secret(KEY_ENV) or "").strip()


def _request(method: str, path: str, *, body: dict[str, Any] | None = None, timeout: int) -> dict[str, Any] | None:
    """JSON request to the Machine Library API; ``None`` on 404."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{API_URL}{path}",
        data=data,
        method=method,
        headers={
            "X-Api-Key": _api_key(),
            "X-Request-Source": "api",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"hermes-machine-library/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise MachineLibraryError(_http_error_detail(error)) from None
    except (urllib.error.URLError, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        raise MachineLibraryError(f"Machine Library is unreachable: {reason}") from None


def _http_error_detail(error: urllib.error.HTTPError) -> str:
    detail = ""
    try:
        payload = json.load(error)
        detail = payload.get("detail") or payload.get("error") or ""
    except Exception:
        pass
    if error.code == 401:
        return f"Machine Library rejected the API key; check {KEY_ENV} (https://machinelibrary.ai/keys)"
    if error.code == 402:
        return f"Machine Library balance is exhausted; top up at https://machinelibrary.ai/keys. {detail}".strip()
    if error.code == 429:
        return "Machine Library rate limit reached; retry shortly"
    return f"HTTP {error.code}" + (f": {detail}" if detail else "")


def _canonical_uri(uris: list[str]) -> str:
    """DOI first, then the first identifier (mirrors the MCP server)."""
    for uri in uris:
        if uri.startswith("doi://"):
            return uri
    return uris[0] if uris else ""


def document_page_url(document_id: str, uris: list[str]) -> str:
    """Public page for a document: ``/r/{DOI}`` when one exists, else ``/r/{id}``."""
    for uri in uris:
        if uri.startswith("doi://"):
            doi = uri.removeprefix("doi://")
            return f"{SITE_URL}/r/" + "/".join(urllib.parse.quote(part, safe="") for part in doi.split("/"))
    return f"{SITE_URL}/r/{urllib.parse.quote(document_id, safe='')}"


def _year(document: dict[str, Any]) -> str:
    issued_at = document.get("issued_at")
    if not isinstance(issued_at, (int, float)):
        return ""
    import datetime

    return str(datetime.datetime.fromtimestamp(issued_at, tz=datetime.timezone.utc).year)


def _byline(document: dict[str, Any]) -> str:
    metadata = document.get("metadata") or {}
    authors = [a.get("name") for a in document.get("authors") or [] if isinstance(a, dict) and a.get("name")]
    parts = [
        str(document.get("type") or "").replace("-", " "),
        ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else ""),
        str(metadata.get("publisher") or metadata.get("container_title") or ""),
        _year(document),
    ]
    return " · ".join(part for part in parts if part)


def _snippet(hit: dict[str, Any]) -> str:
    snippets = hit.get("snippets") or []
    text = next((s.get("text") for s in snippets if isinstance(s, dict) and s.get("text")), None)
    text = text or (hit.get("document") or {}).get("abstract") or ""
    text = " ".join(str(text).split())
    return text[:SNIPPET_CHARS] + ("…" if len(text) > SNIPPET_CHARS else "")


def _hit_to_result(hit: dict[str, Any], position: int) -> dict[str, Any]:
    document = hit.get("document") or {}
    uris = [u for u in document.get("uris") or [] if isinstance(u, str)]
    source_uri = _canonical_uri(uris)
    byline = _byline(document)
    description = " — ".join(part for part in (byline, _snippet(hit)) if part)
    if source_uri:
        description += f" [source: {source_uri}]"
    return {
        "title": document.get("title") or source_uri or "Untitled document",
        "url": document_page_url(str(hit.get("id") or ""), uris),
        "description": description,
        "position": position,
    }


def _document_path(url: str) -> str:
    """API path that fetches ``url``, or raise when Machine Library cannot hold it.

    ``machinelibrary.ai/r/{DOI}`` and ``/r/{id}[/slug]`` map to the stored
    document; any other link or identifier goes to the by-URI resolver.
    """
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme in ("http", "https") and parsed.netloc.removeprefix("www.") == "machinelibrary.ai":
        path = urllib.parse.unquote(parsed.path)
        if not path.startswith("/r/") or len(path) <= 3:
            raise MachineLibraryError("not a Machine Library document page (expected https://machinelibrary.ai/r/...)")
        target = path[3:]
        if target.startswith("10."):
            return _by_uri_path(f"doi://{target}")
        document_id = target.split("/", 1)[0]
        # /v2/documents/{id} caps by tokens; four characters per token is a safe upper bound.
        return f"/v2/documents/{urllib.parse.quote(document_id, safe='')}?max_tokens={EXTRACT_MAX_CHARS // 4}"
    return _by_uri_path(url.strip())


def _by_uri_path(uri: str) -> str:
    return f"/v2/documents/by-uri/{urllib.parse.quote(uri, safe='')}?max_chars={EXTRACT_MAX_CHARS}"


def _extracted(url: str, response: dict[str, Any]) -> dict[str, Any]:
    document = response.get("document") or {}
    uris = [u for u in response.get("uris") or document.get("uris") or [] if isinstance(u, str)]
    title = document.get("title") or ""
    abstract = (document.get("abstract") or "").strip()
    body = (document.get("content") or "").strip()
    content = "\n\n".join(part for part in (f"# {title}" if title else "", abstract, body) if part)
    total = document.get("content_length")
    return {
        "url": url,
        "title": title,
        "content": content,
        "raw_content": content,
        "metadata": {
            "source_uri": _canonical_uri(uris),
            "uris": uris,
            "page_url": document_page_url(str(response.get("id") or ""), uris),
            "type": document.get("type"),
            "byline": _byline(document),
            "content_truncated": isinstance(total, int) and total > len(body),
        },
    }


class MachineLibraryWebSearchProvider(WebSearchProvider):
    """Research search over papers, books, patents and standards, with citable sources."""

    @property
    def name(self) -> str:
        return "machine-library"

    @property
    def display_name(self) -> str:
        return "Machine Library"

    def is_available(self) -> bool:
        try:
            return bool(_api_key())
        except Exception:  # an unscoped secret lookup must not break the picker
            return False

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        query = (query or "").strip()
        if not query:
            return {"success": False, "error": "Machine Library search needs a non-empty query"}
        try:
            limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))
        except (TypeError, ValueError):
            limit = 5
        logger.info("Machine Library search (limit=%d)", limit)
        try:
            response = _request(
                "POST",
                "/v2/search/",
                body={"query": query, "limit": limit},
                timeout=SEARCH_TIMEOUT,
            ) or {}
        except MachineLibraryError as error:
            return {"success": False, "error": f"Machine Library search failed: {error}"}
        hits = [h for h in response.get("hits") or [] if isinstance(h, dict)]
        return {"success": True, "data": {"web": [_hit_to_result(hit, i + 1) for i, hit in enumerate(hits)]}}

    def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for url in urls:
            try:
                response = _request("GET", _document_path(url), timeout=FETCH_TIMEOUT)
                if response is None:
                    raise MachineLibraryError(
                        "no Machine Library document for this URL; use a general web extract backend for it"
                    )
                results.append(_extracted(url, response))
            except MachineLibraryError as error:
                results.append({"url": url, "title": "", "content": "", "raw_content": "", "error": f"Machine Library extract failed: {error}"})
        return results

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "paid",
            "tag": "Research search: papers, books, patents and standards, with citable source links.",
            "env_vars": [
                {
                    "key": KEY_ENV,
                    "prompt": "Machine Library API key",
                    "url": "https://machinelibrary.ai/keys",
                }
            ],
        }
