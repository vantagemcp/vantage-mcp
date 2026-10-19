# Vantage: the AI visibility MCP server

[![Official MCP Registry](https://img.shields.io/badge/MCP%20Registry-dev.vantagemcp%2Fvantage-005a48)](https://registry.modelcontextprotocol.io/v0.1/servers?search=dev.vantagemcp)
[![npm](https://img.shields.io/npm/v/vantage-ai-visibility-mcp)](https://www.npmjs.com/package/vantage-ai-visibility-mcp)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![vantage-mcp MCP server](https://glama.ai/mcp/servers/vantagemcp/vantage-mcp/badges/score.svg)](https://glama.ai/mcp/servers/vantagemcp/vantage-mcp)
[![MCP status](https://mcpi.app/servers/vantage/badge.svg)](https://mcpi.app/servers/vantage)

**Find out whether ChatGPT, Gemini and Perplexity cite your site, and what to change if they don't.**

Vantage is an MCP server. Your coding agent (Claude Code, Cursor, VS Code, or any MCP client) asks it which
questions already cite your domain, who is cited instead, how the winning answer is built, and how your page
differs. No dashboard to read: the answer comes back where you are working.

[**Add to Cursor**](https://vantagemcp.dev/install/cursor/?ref=gh-readme) ·
[**Add to VS Code**](https://vantagemcp.dev/install/vscode/?ref=gh-readme) ·
[Try one check with no account](https://vantagemcp.dev/check?ref=gh-readme)

Claude Code:

```bash
claude mcp add --transport http vantage https://vantagemcp.dev/mcp
```

Free tier: 30 units a month, no card. Sign in with your email the first time a tool runs.

https://github.com/user-attachments/assets/38df5876-16ac-403d-b39f-170a2458cfb1

A real check on vantagemcp.dev, 50 seconds, not sped up. Answers vary run to run: this keyword was cited in 0 of 3 answers earlier today (see the output below) and in 1 of 3 here, which is why Vantage reports cited_runs.


## What a real check looks like

We ran Vantage on its own domain on 19 October 2026: five questions people ask when they look for a tool like
this, three ChatGPT answers per question. Output trimmed to fit; the values are as returned.

```
> Is vantagemcp.dev cited by ChatGPT for these five questions? Read three answers each.

< analyze_citation_coverage(
    domain: "vantagemcp.dev",
    keywords: ["ai visibility mcp server", "check if chatgpt cites my website",
               "llm citation tracking tool", "ai search visibility tool", "mcp server for seo"],
    brand: "Vantage", samples: 3)

{
  "keywords_checked": 5,
  "keywords_cited": 0,
  "coverage_pct": 0.0,
  "results": [
    {
      "keyword": "ai visibility mcp server",
      "cited": false, "cited_runs": 0, "samples_ok": 3,
      "source_frequency": [
        { "domain": "github.com", "runs": 2 },
        { "domain": "www.npmjs.com", "runs": 2 },
        { "domain": "seekinweb.com", "runs": 1 }
      ],
      "previous": { "checked_at": "2026-10-01", "cited_runs": 0 },
      "change": "same"
    },
    {
      "keyword": "llm citation tracking tool",
      "cited": false, "cited_runs": 0, "samples_ok": 3,
      "source_frequency": [
        { "domain": "www.tryprofound.com", "runs": 3 },
        { "domain": "otterly.ai", "runs": 3 },
        { "domain": "ahrefs.com", "runs": 3 }
      ],
      "change": "first_check"
    }
  ]
}
```

Not cited for any of the five, and that is the point of checking. Each result is remembered for 180 days, so
the next run reports `change` and `newly_cited` instead of starting from nothing. From there the agent can call
`analyze_citation_gap` on a page and get an ordered fix brief.

## Tools

Ten tools. Units are what a call costs against your monthly quota (Free: 30, Pro: 500, Scale: 1,500).

| Tool | Answers | Units |
|---|---|---|
| `get_supported_options` | Which engines, platforms and markets each tool accepts, and the per-call limits | 0 |
| `get_usage` | How much of this month's quota is left | 0 |
| `get_check_history` | Your earlier coverage results for a domain, newest first | 0 |
| `find_cited_questions` | Which questions already cite your domain, most asked first | 10 |
| `analyze_citation_coverage` | Which of up to 10 keywords cite a domain, and which do not | 1 per keyword per sample |
| `analyze_citation_gap` | How your page differs from the answer that is cited, plus a fix brief | 1 |
| `analyze_citation_structure` | How the winning answer for a topic is built | 1 per sample |
| `analyze_citation_structure_batch` | The same, across up to 10 related topics | 1 per keyword |
| `find_citation_leaders` | Who dominates AI citations for a topic | 10 |
| `analyze_citation_trend` | Month-by-month citation counts for a domain | 1 per domain |

## Install

**Claude Code, Cursor, VS Code:** use the buttons or the command at the top. All three sign in to Vantage in
your browser on first use.

**Any client that supports MCP sign-in (OAuth):** add `https://vantagemcp.dev/mcp` with no key and sign in with
an email (a free account) or an existing key.

**Any other client:** add to its MCP config:

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

**A client that only starts local commands:** use the npm package
[`vantage-ai-visibility-mcp`](https://www.npmjs.com/package/vantage-ai-visibility-mcp), which connects to the
same hosted server:

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

Get a free API key at [vantagemcp.dev](https://vantagemcp.dev/?ref=gh-readme). To try before installing,
[vantagemcp.dev/check](https://vantagemcp.dev/check?ref=gh-readme) runs a real check with no account, no email
and no card: give it a keyword and it returns the measured shape of the answer ChatGPT cites for it.

Published on the [Official MCP Registry](https://registry.modelcontextprotocol.io/) under the
domain-verified namespace `dev.vantagemcp/vantage`.

## Tools in detail

### `get_supported_options`
Which engines, platforms and markets each tool accepts, and the per-call limits. Costs 0 units; a fixed list.
> "Which markets can I check?"

### `get_usage`
How much of this billing period's quota is left, before spending any of it.
Costs 0 units: reads Vantage's own record, never calls the paid data provider.
> "How many checks do I have left?"

### `find_cited_questions`
Starts from your domain instead of a keyword: the questions where AI answers already cite it, most asked first. 10 units.
> "What does ChatGPT already cite us for?"

### `analyze_citation_coverage`
Which of several prompts actually cite a specific domain, and which don't, up to 10 keywords in one call. ChatGPT, Gemini or Perplexity (`engine`), 1 unit per keyword per sample. Pass `samples=3` to get "cited in 2 of 3 answers" instead of one yes or no. Each result is remembered for 180 days, so the next check reports what changed (`previous`, `change`, `newly_cited`).

Cited and named are reported separately. An answer can name your brand without linking you (`mentioned_not_cited`: the model already knows you, it just isn't citing you) or link a page it never names. Pass `brand` (e.g. `"Notion"`) so the name check looks for your real brand instead of a guess from the domain.
> "Out of everything we care about, where do we already show up?"

### `get_check_history`
Your earlier `analyze_citation_coverage` results for a domain, newest first. 0 units.
> "Did our changes last week move anything?"

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
Diffs your own page's structure against the winning AI-cited answer for the same keyword and returns a `fix_brief`: ordered rewrite instructions your agent can carry out on the page (opening, sections, the answer's points your page does not cover, table, sources), ending with one step beyond the page: which community sites or other websites the answer relies on. The brief never contains the cited answer's text.
> "What should I actually change on this page to get cited?"

### Source mix
Every tool that returns cited domains also returns `source_mix`: how much of them are community sites (Reddit, YouTube, X, Quora and similar) and which. A high share means the topic is won by what people say about a brand elsewhere, not by any one site's pages.

### Country and language
Every tool except `get_usage` takes optional `country` (e.g. `"Italy"`) and `language` (e.g. `"it"`), default United States / `en`. Tools that read a live ChatGPT answer work in any supported market; `find_citation_leaders` and `analyze_citation_trend` only have ChatGPT data for the United States in English, so use `platform: "google"` for other countries.

## What it can and cannot tell you

- **Answers change from run to run.** One answer is a sample, not a verdict. Use `samples=3` before telling anyone
  they are or are not cited.
- **Cited is not the same as named.** Vantage reports a link to your domain and a mention of your brand
  separately, and never merges them.
- **It reads what a person would see.** ChatGPT and Gemini results are the answers a person sees in those apps,
  fetched through DataForSEO; Perplexity results come from its search API. It cannot see private conversations,
  and a result is the answer at the time of the check (`model` and `checked_at` are on every result).
- **A fix brief is a hypothesis.** It shows how your page differs structurally from the answer that is cited.
  Page shape gets a page into the running; being cited is decided mostly by what other sites say about the brand,
  which is why every brief ends with an off-site step.

## Agent skill

[`vantage-citation-audit`](https://github.com/vantagemcp/ai-visibility-skills) teaches your agent the whole loop:
measure which keywords cite you, diagnose one page, apply the fix brief, re-check. It only describes the workflow;
every check is a Vantage tool call.

## Monthly research

Every month Vantage asks ChatGPT, Gemini and Perplexity the same everyday questions and publishes which sites
they cite, by engine and topic, with the raw data:
[vantagemcp.dev/research](https://vantagemcp.dev/research?ref=gh-readme). Free to quote.

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

> **Not to be confused with:** [vantage.sh](https://www.vantage.sh), a cloud cost-management company with its own, unrelated MCP server also named Vantage. Different product, same name.

## Star it

Useful? A star on GitHub helps others find it. Already starred? Tell us what brought you here: [support@vantagemcp.dev](mailto:support@vantagemcp.dev?subject=What%20brought%20me%20to%20Vantage).

## License

MIT
