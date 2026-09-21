# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
versioning follows [Semantic Versioning](https://semver.org/).

## [1.6.0] - 2026-09-21

### Removed
- `check_ai_visibility` is removed. It was deprecated in 1.5.6, and calls to it now fail as an unknown tool. Use `check_prompt_coverage` (cited or not per keyword, 1 unit per keyword), `find_citation_leaders` with `compare_domain` (the domain's rank among the most-cited domains for a topic, 10 units) or `analyze_citation_trend` (month-by-month counts, 1 unit) instead. `dataforseo_client.domain_mentions`, which only it used, is removed too.

### Fixed
- Correction to 1.5.6, which said `analyze_citation_trend` already contains the current count that `check_ai_visibility` returned. It does not: they read different provider endpoints. The total from `check_ai_visibility` was roughly half the sum of the monthly counts and matched no single month, and the provider does not document its time window.
- The `analyze_citation_trend` description no longer calls a zero month "a real measured zero, not a gap". A zero between two large months can be a gap in the provider's history: July 2026 came back as 0 for every large domain we checked while June and August were high.

## [1.5.7] - 2026-09-13

### Added
- `check_prompt_coverage` now reports whether each answer names you, separately from whether it cites you. Each keyword carries `mentioned` (the answer's own text names the domain or brand; domain-only citation links are ignored), and the result adds `keywords_mentioned`, `mentioned_not_cited` (named but not linked) and `mention_terms` (exactly what was looked for). A new optional `brand` argument sets the name to look for; without it the domain's first label is used. It comes from the same provider response, so there is no extra cost. "Cited" and "mentioned" are never merged.

### Fixed
- Metered calls could be refused with "temporarily unavailable" during a burst of checks even when the provider balance was fine, because every call read the provider's balance endpoint first and that endpoint allows only 6 requests a minute per account. The last good reading is now reused for 5 minutes, and a failed lookup falls back to it while it is under 15 minutes old. The $1 floor still applies to the remembered value, and past 15 minutes it fails closed as before.
- `analyze_citation_gap` measured your page's opening from the first block of its markdown, which on many pages is the title plus page chrome (related-article cards, jump labels, bylines), so an opening could be reported as 19 words when it was really a heading and a link. Headings, link-only or image-only lines and short labels before the body are now skipped. On the answer side, a first line that only restates the question ("What is coherent breathing?") is no longer counted as the opening; a heading that carries the answer still is.
- `analyze_citation_gap` could list a broken fragment such as `pubmed.ncbi.nl[Nature](https:` as one of your page's linked domains when the provider's markdown spliced one link inside another. Link URLs are now matched strictly (no whitespace, brackets or angle brackets, parentheses only as a balanced pair), malformed links are skipped, and the domain is read with `urllib.parse.urlsplit`.

### Removed
- `create_api_key_for_stripe_customer` no longer takes an `email` argument, and the free-to-paid in-place upgrade listed under 1.5.4 is gone from this repository. Both had been carried over from the internal codebase by mistake: this repository has never included the billing side of paid signup. Running the paid Stripe path from this package issues a key that records no email. Free self-serve signup by email is unchanged. The hosted service at vantagemcp.dev is not affected.

### Changed
- README: the free tier is described as 30 quota units a month (it previously said 3 checks), and documents the named-versus-cited fields and `brand`.

## [1.5.6] - 2026-09-07

### Deprecated
- `check_ai_visibility` is deprecated, not removed. It still works exactly as before - same cost, same behaviour, same return shape - but its docstring now says so and points elsewhere: `check_prompt_coverage` gives the same cited/not-cited signal per keyword at a tenth of the cost, and `analyze_citation_trend` already contains the current count in its own `months`/`trend` fields. No existing integration needs to change anything; new ones should reach for either of those instead.

## [1.5.5] - 2026-09-07

### Added
- `check_prompt_coverage(domain, keywords)`: which of several prompts actually cite a specific domain, and which don't (up to 10 keywords, 1 unit each). This is usually the real first question of an AI-answer-engine audit - not what a winning answer looks like, but where a domain already shows up and where it is invisible. Reuses the exact-registrable-domain matching added to find_citation_leaders in 1.5.4 (now shared, rather than duplicated).

## [1.5.4] - 2026-09-07

### Added
- `get_usage()`: a new, zero-cost tool that reports the current billing period's usage, limit and remaining units. Added because there is no dashboard anywhere for Vantage - the only previous way to learn the quota existed was to hit it mid-workflow and get denied.

### Fixed
- Every metered tool checked and spent quota before confirming the upstream provider was reachable, so a provider outage or a low prepaid balance charged the customer's quota for an error message. The order is now: confirm the provider is reachable, then spend quota, then call it. Any call that still comes back unusable after that (a malformed response, an upstream exception) has its quota refunded.
- `find_citation_leaders`'s `compare_domain_present` was computed the same way whether the upstream call succeeded or failed, so a parse failure looked identical to "not cited anywhere." It is replaced with `compare_domain_rank` (the domain's position in the results, or null), matched by exact registrable domain rather than substring (a substring match let `notion.so` match `mynotion.so.example.com`). The result also now states `top_domains_limit`, since absence from a top-N list is not evidence of zero citations.
- `analyze_citation_trend` compared the earliest month in its window to the most recent one, and the most recent one is always the current, in-progress month - a domain with a strong previous month could report "flat" simply because the new month had barely started. The current partial month is now excluded from the trend comparison (and flagged when it is), though still returned in the month-by-month list.
- `analyze_citation_structure` (and its batch form) could list the same domain twice, since the provider can cite two different pages on one site separately, which also inflated the reported source count. Domains are now deduplicated before counting.
- All six tool docstrings said the free tier was "3 checks/month total across all tools." The real cap is 30 units/month, spent at different rates by different tools (10 units for the two visibility-lookup tools, 1 unit for the rest) - a free customer could run roughly 10x more of the cheap tools than the docstring implied, understating the free tier by an order of magnitude on the product's only pricing page.
- A customer who signed up on the free tier and later paid with the same email could hit a database error and receive no working key at all, despite being charged: the paid-signup path only checked for an existing key by Stripe customer ID, and a second row with the same email violated a uniqueness constraint. It now upgrades the existing free key in place.

## [1.5.3] - 2026-09-05

### Changed
- No change to the MCP server or any tool. Version bumped so the Official MCP Registry listing, this repository and the published package all agree on one number.

### Fixed
- The Official MCP Registry listing described the server as covering Perplexity. It does not: `VALID_PLATFORMS` is `chat_gpt` and `google`, and the server rejects any other platform value. The listing has been republished with an accurate description, a complete sentence (the previous one was cut off mid-list), and a link to this repository.
- `https://vantagemcp.dev/mcp` returns 401 with a `WWW-Authenticate` header advertising `resource_metadata`, and that URL previously returned 404, so a client following the pointer to find out how to authenticate reached a dead end. It now serves RFC 9728 protected-resource metadata.

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
