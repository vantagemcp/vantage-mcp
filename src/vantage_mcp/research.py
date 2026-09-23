"""Monthly public research: the same 200 keywords asked of every answer
engine, and what the answers cite. Published at vantagemcp.dev/research as
original data an answer engine (or a person) can quote.

run_month() is the monthly job (deploy/vantage-research.timer); aggregate()
and render() are pure, so the page is tested without spending anything.
Only measured fields are kept per answer: never the answer text.
"""

import html
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from vantage_mcp import dataforseo_client as dfs

MIN_BALANCE_USD = 5.0  # a month costs ~$2.60; never let it take the account near the floor
TOP_DOMAINS = 12


def load_keywords(path: str) -> list[tuple[str, str]]:
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                category, keyword = line.split("|", 1)
                out.append((category.strip(), keyword.strip()))
    return out


def _measure(job: tuple[str, str, str]) -> dict:
    category, keyword, engine = job
    row = {"category": category, "keyword": keyword, "engine": engine}
    try:
        r = dfs.citation_structure(keyword, engine=engine)
    except dfs.DataForSEOError:
        return {**row, "error": True}
    if r.get("error"):
        return {**row, "error": True}
    return {**row, "model": r.get("model"), "source_domains": r.get("source_domains") or [],
            "leads_with_list": bool(r.get("leads_with_list")), "has_table": bool(r.get("has_table")),
            "opening_word_count": r.get("opening_word_count") or 0}


def run_month(keywords_path: str, out_dir: str, engines=dfs.ENGINES, workers: int = 8) -> str:
    """Ask every keyword of every engine and write <out_dir>/<YYYY-MM>.json
    and latest.json. Refuses to start below MIN_BALANCE_USD. Returns the path."""
    if dfs.read_balance() < MIN_BALANCE_USD:
        raise SystemExit(f"provider balance under ${MIN_BALANCE_USD}, research run skipped")
    keywords = load_keywords(keywords_path)
    jobs = [(c, k, e) for c, k in keywords for e in engines]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        answers = list(pool.map(_measure, jobs))
    now = datetime.now(timezone.utc)
    data = {"month": now.strftime("%Y-%m"), "generated_at": now.isoformat(timespec="seconds"),
            "keywords": len(keywords), "engines": list(engines), "answers": answers}
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{data['month']}.json")
    for target in (path, os.path.join(out_dir, "latest.json")):
        tmp = target + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, target)
    return path


def _bare(domain: str) -> str:
    return domain.lower().removeprefix("www.")


def _pct(n: int, d: int) -> float:
    return round(100 * n / d, 1) if d else 0.0


def _stats(rows: list[dict]) -> dict:
    ok = [r for r in rows if not r.get("error")]
    with_src = [r for r in ok if r["source_domains"]]
    domains = [d for r in ok for d in r["source_domains"]]
    counts: dict[str, int] = {}
    for r in ok:
        for d in {_bare(x) for x in r["source_domains"]}:
            counts[d] = counts.get(d, 0) + 1
    return {
        "answers": len(ok),
        "failed": len(rows) - len(ok),
        "pct_no_sources": _pct(len(ok) - len(with_src), len(ok)),
        "avg_sources": round(sum(len(r["source_domains"]) for r in with_src) / len(with_src), 1) if with_src else 0,
        "community_pct": _pct(sum(1 for d in domains if dfs.is_community(d)), len(domains)),
        "pct_list_led": _pct(sum(1 for r in ok if r["leads_with_list"]), len(ok)),
        "pct_table": _pct(sum(1 for r in ok if r["has_table"]), len(ok)),
        "avg_opening_words": round(sum(r["opening_word_count"] for r in ok) / len(ok)) if ok else 0,
        "top_domains": [{"domain": d, "answers": n} for d, n in
                        sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_DOMAINS]],
    }


def aggregate(data: dict) -> dict:
    answers = data["answers"]
    engines = data["engines"]
    by_engine = {e: _stats([a for a in answers if a["engine"] == e]) for e in engines}
    categories = sorted({a["category"] for a in answers})
    by_category = {c: _stats([a for a in answers if a["category"] == c]) for c in categories}
    # Do the engines agree? Share of keywords where at least one site is cited
    # by every engine that answered with sources.
    agree = asked = 0
    for kw in sorted({a["keyword"] for a in answers}):
        sets = [{_bare(d) for d in a["source_domains"]} for a in answers
                if a["keyword"] == kw and not a.get("error") and a["source_domains"]]
        if len(sets) == len(engines):
            asked += 1
            agree += bool(set.intersection(*sets))
    return {"month": data["month"], "generated_at": data["generated_at"], "keywords": data["keywords"],
            "engines": engines, "overall": _stats(answers), "by_engine": by_engine,
            "by_category": by_category, "pct_engines_share_a_source": _pct(agree, asked),
            "keywords_compared": asked}


ENGINE_LABELS = {"chat_gpt": "ChatGPT", "gemini": "Gemini", "perplexity": "Perplexity"}


def render(agg: dict, base_url: str) -> tuple[str, str, str]:
    """(title, body_html, head_extra) for the research page."""
    esc = html.escape
    month = datetime.strptime(agg["month"], "%Y-%m").strftime("%B %Y")
    o = agg["overall"]
    eng = agg["engines"]
    label = lambda e: ENGINE_LABELS.get(e, e)  # noqa: E731

    def row(name, key, fmt="{}"):
        return f"<tr><th scope='row'>{name}</th>" + "".join(
            f"<td>{fmt.format(agg['by_engine'][e][key])}</td>" for e in eng) + "</tr>"

    engine_table = (
        "<div class='rs-scroll'><table class='rs-table'><caption>By answer engine</caption><thead><tr><th></th>"
        + "".join(f"<th scope='col'>{label(e)}</th>" for e in eng) + "</tr></thead><tbody>"
        + row("Answers measured", "answers")
        + row("Share of cited sites that are community sites", "community_pct", "{}%")
        + row("Average sites cited, when any are", "avg_sources")
        + row("Answers citing no sites at all", "pct_no_sources", "{}%")
        + row("Answers that open with a list", "pct_list_led", "{}%")
        + row("Answers with a table", "pct_table", "{}%")
        + row("Average opening length (words)", "avg_opening_words")
        + "</tbody></table></div>")

    cats = sorted(agg["by_category"].items(), key=lambda kv: -kv[1]["community_pct"])
    cat_table = ("<div class='rs-scroll'><table class='rs-table'><caption>By topic, all engines together</caption><thead><tr>"
                 "<th scope='col'>Topic</th><th scope='col'>Community share</th>"
                 "<th scope='col'>Average sites cited</th><th scope='col'>Most cited site</th></tr></thead><tbody>"
                 + "".join(f"<tr><th scope='row'>{esc(c)}</th><td>{s['community_pct']}%</td>"
                           f"<td>{s['avg_sources']}</td>"
                           f"<td>{esc(s['top_domains'][0]['domain']) if s['top_domains'] else '-'}</td></tr>"
                           for c, s in cats) + "</tbody></table></div>")

    tops = "".join(
        f"<div class='rs-top'><h3>{label(e)}</h3><ol>"
        + "".join(f"<li>{esc(t['domain'])} <span>{t['answers']}</span></li>"
                  for t in agg["by_engine"][e]["top_domains"][:8]) + "</ol></div>" for e in eng)

    headline = (f"Across {o['answers']} AI answers to {agg['keywords']} everyday questions in "
                f"{month}, {o['community_pct']}% of the sites cited were community sites such as "
                f"Reddit, YouTube and Quora, and the three engines cited at least one common site "
                f"for only {agg['pct_engines_share_a_source']}% of the questions.")

    body = f"""<h1>What AI answer engines cite: {esc(month)}</h1>
<p class="rs-lede">{esc(headline)}</p>
<p class="check-note">Measured by Vantage on {esc(agg['generated_at'][:10])}. The same
{agg['keywords']} questions go to ChatGPT, Gemini and Perplexity every month, so the numbers
can be compared month to month. <a href="{base_url}/research/data.json">Download the data</a> (JSON).</p>
{engine_table}
<h2>The sites each engine cites most</h2>
<p>Number of the {agg['keywords']} answers citing each site at least once.</p>
<div class="rs-tops">{tops}</div>
<h2>By topic</h2>
{cat_table}
<h2>How this is measured</h2>
<p>Each question is asked once per engine, in English, as from the United States. ChatGPT and
Gemini are read the way a person sees them in those apps; Perplexity through its own sonar API
with web search on. For every answer we keep only measurements: which sites it cites, whether
it opens with a list, whether it has a table, and how long its opening is. Never the answer text.
"Community sites" are sites whose content is posted by their users (Reddit, YouTube, X, Quora,
LinkedIn, Medium, review sites and similar). A site counts once per answer however many of its
pages are cited. Answers change from run to run, so treat single rows as a snapshot and the
totals as the finding. {o['failed']} of {o['answers'] + o['failed']} requests failed and are left out.</p>
<p>Check your own site against the same engines with Vantage's
<a href="{base_url}/docs/">MCP tools</a>, or try one keyword free at <a href="{base_url}/check">/check</a>.</p>"""

    dataset = {
        "@context": "https://schema.org", "@type": "Dataset",
        "name": f"What AI answer engines cite, {month}",
        "description": headline,
        "url": f"{base_url}/research",
        "temporalCoverage": agg["month"],
        "creator": {"@type": "Organization", "name": "Vantage", "url": base_url},
        "distribution": [{"@type": "DataDownload", "encodingFormat": "application/json",
                          "contentUrl": f"{base_url}/research/data.json"}],
        "variableMeasured": ["community share of cited sites", "sites cited per answer",
                             "answers opening with a list", "answers with a table"],
    }
    head = (f'<meta name="description" content="{esc(headline[:155], quote=True)}">\n'
            f'<link rel="canonical" href="{base_url}/research">\n'
            f'<script type="application/ld+json">{json.dumps(dataset)}</script>')
    return f"What AI answer engines cite, {month}", body, head


RESEARCH_CSS = """
.rs-lede { font-size: 1.15rem; color: var(--ink); }
.rs-scroll { overflow-x: auto; }
.rs-table { width: 100%; border-collapse: collapse; margin: 1.2rem 0 2rem; font-size: 0.95rem; }
.rs-table caption { text-align: left; font-weight: 600; margin-bottom: 0.5rem; color: var(--ink); }
.rs-table th, .rs-table td { padding: 0.55rem 0.5rem; border-bottom: 1px solid var(--line, #22403c); text-align: left; }
.rs-table td { font-variant-numeric: tabular-nums; }
.rs-table thead th { color: var(--muted); font-weight: 500; }
.rs-tops { display: grid; grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr)); gap: 1rem; margin: 1rem 0 2rem; }
.rs-top h3 { margin: 0 0 0.4rem; font-size: 1rem; }
.rs-top ol { margin: 0; padding-left: 1.3rem; }
.rs-top li { padding: 0.15rem 0; overflow-wrap: anywhere; }
.rs-top li span { color: var(--muted); font-size: 0.85em; margin-left: 0.3rem; }
@media (max-width: 560px) { .rs-table { font-size: 0.85rem; } .rs-table th, .rs-table td { padding: 0.45rem 0.3rem; } }
"""


if __name__ == "__main__":
    import sys
    print(run_month(sys.argv[1], sys.argv[2]))
