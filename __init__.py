"""Machine Library web search + extract provider plugin for Hermes Agent."""

from __future__ import annotations

try:
    from .provider import MachineLibraryWebSearchProvider
except ImportError:  # loaded by file path, without a parent package
    from provider import MachineLibraryWebSearchProvider  # type: ignore[no-redef]


def register(ctx) -> None:
    """Hermes plugin entry point."""
    ctx.register_web_search_provider(MachineLibraryWebSearchProvider())
