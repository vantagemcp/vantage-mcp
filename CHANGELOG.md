# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
versioning follows [Semantic Versioning](https://semver.org/).

## [1.1.3] - 2026-08-21

### Changed
- All 4 tools now declare MCP annotations (`read_only_hint`, `destructive_hint`, `idempotent_hint`, `open_world_hint`) - none of them write anything or have side effects, so agents can rely on structured metadata instead of inferring it from prose.
- Every tool's docstring now states its read-only/retry-safe status and quota cost explicitly, documents its actual return shape field-by-field, and names the most relevant sibling tool for when this one isn't the right fit.

## [1.1.1] - 2026-08-20

### Fixed
- `check_ai_visibility`, `citation_leaders`, and single-keyword `citation_structure` now catch transient provider errors instead of crashing the whole tool call with an unhandled exception.
- `check_ai_visibility` no longer treats an unparseable provider response the same as a confirmed zero-mentions result - it now returns a distinct error.

## [1.1.0] - 2026-08-14

### Added
- `citation_structure_batch`: analyze up to 10 related keywords in one call, for content planning across a topic cluster. Per-keyword provider errors degrade to a per-keyword error entry instead of failing the whole batch.
- `Dockerfile` for directory scanner compatibility (stdio entrypoint).
- Disambiguation note in the README re: vantage.sh, an unrelated cloud cost-management company with its own, different MCP server also named Vantage.

### Changed
- Usage metering switched from flat per-tool call counts to weighted cost-units. `citation_structure`/`citation_structure_batch` cost 1 unit/call, `check_ai_visibility`/`citation_leaders` cost 10 units/call, matching their real provider-cost ratio (~25-37x difference) - a free-tier user is no longer capped the same whether they use the cheap or expensive tools.

## [1.0.0] - 2026-08-14

### Added
- Initial release: `check_ai_visibility`, `citation_leaders`, `citation_structure` tools.
- Bearer-token auth and usage metering (free/pro/team tiers) over the hosted Streamable HTTP transport.
- Published to the Official MCP Registry as `dev.vantagemcp/vantage`, domain-verified namespace.
