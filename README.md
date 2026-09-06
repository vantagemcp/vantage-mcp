# Vantage

[![vantage-mcp MCP server](https://glama.ai/mcp/servers/vantagemcp/vantage-mcp/badges/score.svg)](https://glama.ai/mcp/servers/vantagemcp/vantage-mcp)

Know if AI actually cites you.

Vantage is an MCP server that checks whether ChatGPT and Google's AI Overview cite your brand, callable directly from Claude Code, Cursor, or any MCP client. No dashboard to interpret, just a straight answer.

## Try it without installing anything

[vantagemcp.dev/check](https://vantagemcp.dev/check) runs a real citation check with no account, no email and no card. Give it a keyword and it returns the measured shape of the answer ChatGPT actually cites for it: whether the answer opens with a list, how long the opening is, how many sources it cites, and which domains those are.

Every answer keeps a permanent page, and they are all listed in [check/sitemap.xml](https://vantagemcp.dev/check/sitemap.xml).

Published on the [Official MCP Registry](https://registry.modelcontextprotocol.io/) under the domain-verified namespace `dev.vantagemcp/vantage`.

> **Not to be confused with:** [vantage.sh](https://www.vantage.sh), a cloud cost-management company with its own, unrelated MCP server also named Vantage. Different product, same name.

## Install

Add to your MCP client config:

```json
{
  "mcpServers": {
    "vantage": {
      "url": "https://vantagemcp.dev/mcp",
      "headers": { "Authorization": "Bearer YOUR_API_KEY" }
    }
  }
}
```

Try it first with no account at all at [vantagemcp.dev/check](https://vantagemcp.dev/check), or get a free API key (3 checks/month, no card required) at [vantagemcp.dev](https://vantagemcp.dev).

## Tools

### `get_usage`
How much of this billing period's quota is left, before spending any of it.
Costs 0 units - reads Vantage's own record, never calls the paid data provider.
> "How many checks do I have left?"

### `check_ai_visibility` — deprecated
Still works, but costs 10 units for a bare count with no context. Use
`check_prompt_coverage` (1 unit/keyword) or `analyze_citation_trend`
(already includes the current count) instead.

### `check_prompt_coverage`
Which of several prompts actually cite a specific domain, and which don't - up to 10 keywords in one call.
> "Out of everything we care about, where do we already show up?"

### `find_citation_leaders`
Who dominates AI-answer citations for a topic, and whether a domain is among them.
> "Who's winning AI search for this?"

### `analyze_citation_trend`
Month-by-month mention counts for a domain, so you can see whether visibility is growing or fading.
> "Is our AI visibility improving?"

### `analyze_citation_structure`
How the winning AI answer for a topic is actually shaped: list-led, sources cited, opening length.
> "What does a winning answer look like?"

### `analyze_citation_structure_batch`
Same as `analyze_citation_structure`, across up to 10 related topics in one call, for content planning across a cluster.
> "What do winning answers look like across this whole topic cluster?"

### `analyze_citation_gap`
Diffs your own page's structure against the winning AI-cited answer for the same keyword, so you get concrete gaps to close instead of just the winner's shape.
> "What should I actually change on this page to get cited?"

## Example

```
> agent calls find_citation_leaders(
  keyword: "best mood tracker app",
  platform: "chat_gpt"
)

< response
{
  "keyword": "best mood tracker app",
  "platform": "chat_gpt",
  "top_domains": [
    { "domain": "www.reddit.com", "mentions": 59 },
    { "domain": "apps.apple.com", "mentions": 55 },
    { "domain": "en.wikipedia.org", "mentions": 49 },
    { "domain": "www.makeuseof.com", "mentions": 12 },
    { "domain": "play.google.com", "mentions": 11 }
  ]
}
```

## Running your own instance

This repo is the MCP server itself: API-key auth, usage metering, one tool per read (see Tools above). Billing and account provisioning are a separate internal service, not included here.

```bash
uv pip install -e .
export DATAFORSEO_USERNAME=...
export DATAFORSEO_PASSWORD=...

# stdio (local MCP client, e.g. Claude Desktop config pointing at this command)
python -m vantage_mcp.server

# streamable-http (network service, bind to loopback behind your own reverse proxy)
export VANTAGE_PORT=8420
python -m vantage_mcp.server --http
```

Requires a [DataForSEO](https://dataforseo.com/) account for the underlying SERP/AI-answer data.

## License

MIT
