# Machine Library for Hermes Agent

Research search for Hermes's built-in `web_search` and `web_extract` tools.
[Machine Library](https://machinelibrary.ai) searches papers, books, patents,
standards and Wikipedia, and returns reranked results with links to a stable
document page, so Hermes can cite where a claim comes from.

- **`web_search`**: results from the scholarly corpus. Each result has the
  title, document type, authors, publisher and year, the best-matching
  passage, the canonical source identifier (for example
  `doi://10.48550/arXiv.1706.03762` or `iso://1219-1:2012`), and a
  `https://machinelibrary.ai/r/...` page link.
- **`web_extract`**: full text, up to 20,000 characters, for Machine Library
  document pages and for source links Machine Library can resolve (DOI and
  doi.org links, arXiv, standard identifiers). Other URLs fail one by one with
  a clear message. Pair Machine Library with a general extract backend for the
  open web.

## Install

```bash
hermes plugins install machine-library
```

Hermes asks for `MACHINELIBRARY_API_KEY`. Create a key at
<https://machinelibrary.ai/keys>; searches use prepaid credits.

Then select the backend with `hermes tools`, or in `~/.hermes/config.yaml`:

```yaml
web:
  backend: machine-library
```

To keep a general web backend for pages and use Machine Library only for
search:

```yaml
web:
  search_backend: machine-library
  extract_backend: firecrawl
```

## Pricing

Requests are billed to your Machine Library balance:

- **Search:** $0.01 plus $0.001 per result, so about $0.015 for Hermes's
  default 5 results.
- **Extract:** $0.003 per 4,096 tokens of returned text.

See <https://machinelibrary.ai/pricing>.

## What this plugin does on your machine

- **Network:** sends search queries and URLs to extract to
  `https://api.machinelibrary.ai`, operated by Space Frontiers, with your API
  key. Nothing else is contacted.
- **Credentials:** reads `MACHINELIBRARY_API_KEY` only through Hermes's
  `agent.secret_scope.get_secret`. It never reads `.env` or other credential
  files.
- **No telemetry, background processes, shell commands or local file
  access.** Standard library only; no Python dependencies.

Machine Library's privacy policy: <https://machinelibrary.ai/privacy/world>.

## MCP alternative

Machine Library also runs a hosted MCP server at
`https://mcp.machinelibrary.ai`, with in-document passage search,
public-discussion search and research answers. Add it under `mcp_servers` in
`~/.hermes/config.yaml`; it signs in with OAuth or an
`Authorization: Bearer <API key>` header.

## Development

```bash
pytest tests          # offline; needs hermes-agent importable (PYTHONPATH)
hermes plugins validate --install-deps .
```

## License

MIT
