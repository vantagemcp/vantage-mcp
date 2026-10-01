# vantage-ai-visibility-mcp

Vantage is an AI visibility MCP server that checks whether ChatGPT, Gemini, Perplexity and Google AI Overviews cite your site.

Know if AI actually cites you: for which questions, who is cited instead, and whether that is changing. Call it directly from Claude Code, Cursor, or any MCP client.

This package connects clients that start MCP servers as a local command to the hosted server at `https://vantagemcp.dev/mcp`. If your client takes a remote URL, add that URL directly instead; you do not need this package.

## Use it

Sign in from the browser on first run (free account from your email):

```json
{
  "mcpServers": {
    "vantage": { "command": "npx", "args": ["-y", "vantage-ai-visibility-mcp"] }
  }
}
```

Or with an API key:

```json
{
  "mcpServers": {
    "vantage": {
      "command": "npx",
      "args": ["-y", "vantage-ai-visibility-mcp"],
      "env": { "VANTAGE_API_KEY": "YOUR_API_KEY" }
    }
  }
}
```

## Tools

`check_prompt_coverage`, `find_cited_questions`, `find_citation_leaders`, `analyze_citation_trend`, `analyze_citation_structure`, `analyze_citation_structure_batch`, `analyze_citation_gap`, `get_check_history`, `get_usage`. Full reference: [vantagemcp.dev/docs](https://vantagemcp.dev/docs/).

Source: [github.com/vantagemcp/vantage-mcp](https://github.com/vantagemcp/vantage-mcp). Not affiliated with vantage.sh.
