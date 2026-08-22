# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
versioning follows [Semantic Versioning](https://semver.org/).

## [1.5.2] - 2026-08-22

### Added
- The structured call-outcome log is now also mirrored to a plain file (in addition to the existing journald capture), so an external health-check process can read recent outcomes without journal-read permissions.

## [1.5.1] - 2026-08-22

### Fixed
- The provider-balance check (`read_balance`) could raise a raw, uncaught `TypeError`/`KeyError` on a malformed response from DataForSEO's balance endpoint, bypassing the graceful "temporarily unavailable" fallback every other provider call already had. Now wrapped like every other client call, so a malformed response degrades the same way a provider error does.

## [1.5.0] - 2026-08-21

### Added
- `analyze_citation_gap(keyword, your_url)`: diffs your own page's structure against the AI-cited winning answer for the same keyword and returns concrete gaps to close (e.g. "winning answer cites 4 sources, your page links out to 0"), instead of only describing the winner like `analyze_citation_structure` does. Reuses that same structural parser against your page's content (fetched via DataForSEO's OnPage Content Parsing endpoint). Costs 1 quota unit/call.

## [1.4.0] - 2026-08-21

### Changed (BREAKING)
- Renamed `citation_leaders` -> `find_citation_leaders`. Glama's naming-consistency check flagged it as the only tool without a verb prefix, now that `citation_structure`(`_batch`) are `analyze_*`. Anyone with the old name in an MCP client config needs to update it - there is no alias/fallback for the old name.

### Fixed
- Two stale tool-count references (a code comment claiming "4 tools" and a README line claiming "three tools") - both left over from before `analyze_citation_trend` brought the count to 5.

## [1.3.1] - 2026-08-21

### Changed
- Trimmed `analyze_citation_trend`'s docstring: dropped a backstory clause about DataForSEO's historical endpoint pricing at $0 that duplicated context already covered elsewhere and added nothing the calling agent needs. Now matches the concise cost-line pattern used by the other three tools.

## [1.3.0] - 2026-08-21

### Added
- `analyze_citation_trend(domain, platform, months)`: month-by-month AI-citation counts for a domain since DataForSEO's history began (2025-08-01), plus a simple up/down/flat trend summary. Costs 1 quota unit/call - DataForSEO's historical endpoint has priced at $0 on every real call made building this, unlike the two per-lookup tools. A month with no tracked citations normalizes to an explicit 0 rather than being silently omitted (found calling the endpoint live: it drops the whole "metrics" object for a zero month instead of returning zeros).

## [1.2.1] - 2026-08-21

### Fixed
- `check_ai_visibility` and `citation_leaders` claimed `platform` accepted "perplexity" and "gemini" since v1.0.0 - it never did. The underlying DataForSEO endpoints only support `chat_gpt` and `google` (Google's AI Overview); requesting anything else would have errored or silently returned nothing meaningful. Both tools now validate `platform` up front and return a clear error naming the two real options instead of passing a bad value through. Docs, descriptions, and the README updated to match reality.

## [1.2.0] - 2026-08-21

### Changed (BREAKING)
- Renamed `citation_structure` -> `analyze_citation_structure` and `citation_structure_batch` -> `analyze_citation_structure_batch`. Glama's naming-consistency check flagged these two as the only tools with no verb, inconsistent with `check_ai_visibility`/`citation_leaders`. Anyone with the old names in an MCP client config needs to update them - there is no alias/fallback for the old names.

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
