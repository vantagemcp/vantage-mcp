# AGENTS.md

Notes for coding agents working in this repository.

## What this is

Vantage is a Python MCP server that checks whether AI answer engines (ChatGPT, Gemini, Perplexity, Google AI
Overviews) cite a domain. It exposes ten read-only tools. The hosted copy runs at `https://vantagemcp.dev/mcp`;
this repo is the server only. Billing and account provisioning live in a separate internal service.

## Layout

- `src/vantage_mcp/server.py`: tool definitions (`@mcp.tool`), the stdio and `--http` (Streamable HTTP) transports.
- `src/vantage_mcp/dataforseo_client.py`: calls to the data provider and parsing of the answers it returns.
- `src/vantage_mcp/auth.py`, `keys.py`, `store.py`: OAuth sign-in, API keys, SQLite usage and saved checks.
- `src/vantage_mcp/research.py`: the monthly research run. `mailer.py`: the email that sends a copy of a key at sign-in.
- `npm/`: the stdio wrapper published as `vantage-ai-visibility-mcp`; it connects to the hosted server.

## Run and check

```bash
uv venv && uv pip install -e .
export DATAFORSEO_USERNAME=... DATAFORSEO_PASSWORD=...
python -m vantage_mcp.server          # stdio
python -m vantage_mcp.server --http   # Streamable HTTP on VANTAGE_PORT (default 8420)
```

CI here only checks that `vantage_mcp.server` imports. The fuller test suite is kept with the maintainers' working
copy, so say in your pull request what you ran.

## Rules that matter

- Every tool is read-only and returns structured data. Keep new tools read-only and annotated like the others.
- Metering happens before the paid provider call. A call that is rejected must never cost money.
- A tool's unit cost appears in its docstring and in the README tables. Change them together.
- `analyze_citation_gap`'s `fix_brief` never contains the cited answer's text. Keep it that way.
- Never commit credentials. Provider credentials come from the environment.
