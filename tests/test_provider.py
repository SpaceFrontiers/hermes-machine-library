"""Offline tests: the HTTP layer is replaced, Hermes's provider contract is real."""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import provider  # noqa: E402
from provider import MachineLibraryWebSearchProvider, document_page_url  # noqa: E402

SEARCH_RESPONSE = {
    "trajectory": {"id": "t", "feedback_token": "f"},
    "hits": [
        {
            "id": "doc1",
            "score": 0.9,
            "snippets": [{"field": "content", "text": "The  Transformer\nuses attention."}],
            "document": {
                "title": "Attention Is All You Need",
                "type": "journal-article",
                "authors": [{"name": "A"}, {"name": "B"}, {"name": "C"}, {"name": "D"}],
                "issued_at": 1497225600,
                "uris": ["arxiv://1706.03762", "doi://10.48550/arXiv.1706.03762"],
            },
        },
        {
            "id": "iso1",
            "score": 0.8,
            "snippets": [],
            "document": {
                "title": "Fluid power systems",
                "type": "standard",
                "abstract": "Graphical symbols.",
                "metadata": {"publisher": "ISO"},
                "uris": ["iso://1219-1:2012"],
            },
        },
    ],
}


class Calls(list):
    routes: dict


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def calls(monkeypatch):
    """Record requests and answer from ``routes`` (path prefix -> status, body)."""
    recorded = Calls()
    routes: dict = {}

    def fake_urlopen(request, timeout):
        recorded.append(request)
        path = request.full_url.removeprefix(provider.API_URL)
        for prefix, (status, body) in routes.items():
            if path.startswith(prefix):
                payload = json.dumps(body).encode()
                if status >= 400:
                    raise urllib.error.HTTPError(request.full_url, status, "err", {}, io.BytesIO(payload))
                return FakeResponse(payload)
        raise AssertionError(f"unexpected request {path}")

    monkeypatch.setattr(provider.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(provider, "get_secret", lambda name, default=None: "ml_test" if name == provider.KEY_ENV else default)
    recorded.routes = routes
    return recorded


def test_availability_follows_the_key(monkeypatch):
    p = MachineLibraryWebSearchProvider()
    monkeypatch.setattr(provider, "get_secret", lambda name, default=None: None)
    assert not p.is_available()
    monkeypatch.setattr(provider, "get_secret", lambda name, default=None: "ml_x")
    assert p.is_available()

    def unscoped(name, default=None):
        raise RuntimeError("no profile scope")

    monkeypatch.setattr(provider, "get_secret", unscoped)
    assert not p.is_available()


def test_search_maps_hits_to_the_web_envelope(calls):
    calls.routes["/v2/search/"] = (200, SEARCH_RESPONSE)
    result = MachineLibraryWebSearchProvider().search("transformer attention", limit=50)

    request = calls[0]
    assert request.get_method() == "POST"
    assert request.get_header("X-api-key") == "ml_test"
    assert request.get_header("X-request-source") == "api"
    body = json.loads(request.data)
    assert body == {"query": "transformer attention", "limit": 20, "index_names": ["documents"]}

    assert result["success"] is True
    first, second = result["data"]["web"]
    assert first["title"] == "Attention Is All You Need"
    assert first["url"] == "https://machinelibrary.ai/r/10.48550/arXiv.1706.03762"
    assert first["position"] == 1
    assert "journal article · A, B, C et al. · 2017" in first["description"]
    assert "The Transformer uses attention." in first["description"]
    assert first["description"].endswith("[source: doi://10.48550/arXiv.1706.03762]")
    assert second["url"] == "https://machinelibrary.ai/r/iso1"
    assert "standard · ISO" in second["description"]
    assert "Graphical symbols." in second["description"]


def test_search_reports_api_errors_without_raising(calls):
    calls.routes["/v2/search/"] = (401, {"detail": "bad key"})
    result = MachineLibraryWebSearchProvider().search("x")
    assert result["success"] is False
    assert "MACHINELIBRARY_API_KEY" in result["error"]

    calls.routes["/v2/search/"] = (402, {"detail": "Balance is $0.00"})
    assert "top up" in MachineLibraryWebSearchProvider().search("x")["error"]


def test_empty_query_is_rejected_locally(calls):
    assert MachineLibraryWebSearchProvider().search("  ")["success"] is False
    assert calls == []


@pytest.mark.parametrize(
    ("url", "expected_path"),
    [
        ("https://machinelibrary.ai/r/10.48550/arXiv.1706.03762", "/v2/documents/by-uri/doi%3A%2F%2F10.48550%2FarXiv.1706.03762?max_chars=20000"),
        ("https://machinelibrary.ai/r/abc123/some-title-slug", "/v2/documents/abc123?max_tokens=5000"),
        ("https://www.machinelibrary.ai/r/abc123", "/v2/documents/abc123?max_tokens=5000"),
        ("https://arxiv.org/abs/1706.03762", "/v2/documents/by-uri/https%3A%2F%2Farxiv.org%2Fabs%2F1706.03762?max_chars=20000"),
        ("doi://10.1038/nature14539", "/v2/documents/by-uri/doi%3A%2F%2F10.1038%2Fnature14539?max_chars=20000"),
    ],
)
def test_extract_routes_urls_to_documents(calls, url, expected_path):
    calls.routes["/v2/documents/"] = (
        200,
        {
            "id": "abc123",
            "uris": ["doi://10.1/x"],
            "document": {"title": "T", "abstract": "Abs.", "content": "Body.", "content_length": 99, "type": "book"},
        },
    )
    [page] = MachineLibraryWebSearchProvider().extract([url])
    assert calls[0].full_url == provider.API_URL + expected_path
    assert page["url"] == url
    assert page["title"] == "T"
    assert page["content"] == "# T\n\nAbs.\n\nBody."
    assert page["raw_content"] == page["content"]
    assert page["metadata"]["source_uri"] == "doi://10.1/x"
    assert page["metadata"]["content_truncated"] is True
    assert "error" not in page


def test_extract_fails_per_url(calls):
    calls.routes["/v2/documents/by-uri/"] = (404, {"detail": "not found"})
    pages = MachineLibraryWebSearchProvider().extract(["https://example.com/a", "https://machinelibrary.ai/search"])
    assert [p["url"] for p in pages] == ["https://example.com/a", "https://machinelibrary.ai/search"]
    assert "general web extract backend" in pages[0]["error"]
    assert "not a Machine Library document page" in pages[1]["error"]
    assert len(calls) == 1  # the non-document page never reaches the API


def test_document_page_url_prefers_doi():
    assert document_page_url("id1", ["isbn://1", "doi://10.1/a b"]) == "https://machinelibrary.ai/r/10.1/a%20b"
    assert document_page_url("id 1", ["isbn://1"]) == "https://machinelibrary.ai/r/id%201"


def test_register_hands_hermes_a_web_search_provider():
    import importlib.util

    from agent.web_search_provider import WebSearchProvider

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("machine_library_plugin", root / "__init__.py", submodule_search_locations=[str(root)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    registered = []

    class Ctx:
        def register_web_search_provider(self, p):
            registered.append(p)

    module.register(Ctx())
    [p] = registered
    assert isinstance(p, WebSearchProvider)
    assert p.name == "machine-library"
    assert p.supports_search() and p.supports_extract()
