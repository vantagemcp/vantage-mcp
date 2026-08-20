# Vantage

[![vantage-mcp MCP server](https://glama.ai/mcp/servers/vantagemcp/vantage-mcp/badges/score.svg)](https://glama.ai/mcp/servers/vantagemcp/vantage-mcp)

Know if AI actually cites you.

Vantage is an MCP server that checks whether ChatGPT, Perplexity, and Gemini cite your brand, callable directly from Claude Code, Cursor, or any MCP client. No dashboard to interpret, just a straight answer.

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

Get a free API key (3 checks/month, no card required) at [vantagemcp.dev](https://vantagemcp.dev).

## Tools

### `check_ai_visibility`
Is a domain cited at all, on a given AI platform.
> "Does ChatGPT know about us?"

### `citation_leaders`
Who dominates AI-answer citations for a topic, and whether a domain is among them.
> "Who's winning AI search for this?"

### `citation_structure`
How the winning AI answer for a topic is actually shaped: list-led, sources cited, opening length.
> "What does a winning answer look like?"

### `citation_structure_batch`
Same as `citation_structure`, across up to 10 related topics in one call, for content planning across a cluster.
> "What do winning answers look like across this whole topic cluster?"

## Example

```
> agent calls citation_leaders(
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

This repo is the MCP server itself: three tools, API-key auth, usage metering. Billing and account provisioning are a separate internal service, not included here.

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
