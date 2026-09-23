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
    # One bad answer must never sink a 600-answer run: anything that goes
    # wrong becomes a failed row (counted and shown on the page), with its
    # message kept for diagnosis.
    try:
        r = dfs.citation_structure(keyword, engine=engine)
    except Exception as e:  # noqa: BLE001
        return {**row, "error": f"{type(e).__name__}: {e}"[:200]}
    if r.get("error"):
        return {**row, "error": str(r["error"])[:200]}
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
    failed = [a for a in answers if a.get("error")]
    reasons: dict[str, int] = {}
    for a in failed:
        reasons[a["error"]] = reasons.get(a["error"], 0) + 1
    print(f"{len(answers) - len(failed)} of {len(answers)} answers measured; failures: {reasons}", flush=True)
    if len(failed) * 2 > len(answers):
        raise SystemExit("more than half the answers failed; not publishing this month over the last good one")
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


def _answer_counts(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        if not r.get("error"):
            for d in {_bare(x) for x in r["source_domains"]}:
                counts[d] = counts.get(d, 0) + 1
    return counts


def aggregate(data: dict) -> dict:
    answers = data["answers"]
    engines = data["engines"]
    by_engine = {e: _stats([a for a in answers if a["engine"] == e]) for e in engines}
    categories = sorted({a["category"] for a in answers})
    by_category = {c: _stats([a for a in answers if a["category"] == c]) for c in categories}
    by_category_engine = {c: {e: _stats([a for a in answers if a["category"] == c and a["engine"] == e])["community_pct"]
                              for e in engines} for c in categories}
    counts = {e: _answer_counts([a for a in answers if a["engine"] == e]) for e in engines}
    # Do the engines agree? Per keyword where every engine answered with
    # sources: is at least one site cited by all of them?
    agreement = []
    for kw in sorted({a["keyword"] for a in answers}):
        rows = [a for a in answers if a["keyword"] == kw and not a.get("error") and a["source_domains"]]
        if len(rows) == len(engines):
            shared = set.intersection(*[{_bare(d) for d in a["source_domains"]} for a in rows])
            agreement.append({"keyword": kw, "category": rows[0]["category"], "shared": sorted(shared)})
    agree = sum(1 for a in agreement if a["shared"])
    # Each engine's favourite site, and how often the other engines cite it.
    favourites = {e: {"domain": s["top_domains"][0]["domain"], "answers": s["top_domains"][0]["answers"],
                      "elsewhere": {o: counts[o].get(s["top_domains"][0]["domain"], 0) for o in engines if o != e}}
                  for e, s in by_engine.items() if s["top_domains"]}
    return {"month": data["month"], "generated_at": data["generated_at"], "keywords": data["keywords"],
            "engines": engines, "overall": _stats(answers), "by_engine": by_engine,
            "by_category": by_category, "by_category_engine": by_category_engine,
            "agreement": agreement, "favourites": favourites,
            "pct_engines_share_a_source": _pct(agree, len(agreement)), "keywords_compared": len(agreement)}


ENGINE_LABELS = {"chat_gpt": "ChatGPT", "gemini": "Gemini", "perplexity": "Perplexity"}
# Engine identity colours: Vantage's teal and orange plus a blue, all at OKLCH
# L 0.66 so they sit in the dark-mode band. Validated 2026-09-24 against the
# site surface with the dataviz palette validator (all pairs, CVD dE >= 13.1,
# normal >= 16.4, contrast >= 3:1). Every mark is also labelled with the
# engine's name, so colour is never the only cue.
ENGINE_COLORS = {"chat_gpt": "#20a992", "gemini": "#6591e1", "perplexity": "#d6760c"}


def next_edition(month: str) -> str:
    y, m = (int(x) for x in month.split("-"))
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return datetime(y, m, 1).strftime("1 %B %Y")


def _findings(agg: dict) -> list[str]:
    """Plain sentences computed from this month's numbers, so the page never
    claims something the data does not show."""
    esc = html.escape
    be, eng = agg["by_engine"], agg["engines"]
    label = lambda e: ENGINE_LABELS.get(e, e)  # noqa: E731
    out = []
    most = max(eng, key=lambda e: be[e]["avg_sources"])
    least = min(eng, key=lambda e: be[e]["avg_sources"])
    if most != least:
        out.append(f"<strong>{label(most)} cites the most sites</strong>: {be[most]['avg_sources']} per answer, "
                   f"against {be[least]['avg_sources']} on {label(least)}.")
    fav = agg["favourites"]
    comm = [e for e in eng if e in fav and dfs.is_community(fav[e]["domain"])]
    pick = comm[0] if comm else max(eng, key=lambda e: be[e]["community_pct"])
    if pick in fav:
        f = fav[pick]
        (o1, n1), *rest = f["elsewhere"].items()
        others = f"{label(o1)} cites it in {n1} {'answer' if n1 == 1 else 'answers'}" + "".join(
            f", {label(o)} in {n}" for o, n in rest)
        out.append(f"<strong>{esc(f['domain'])} is {label(pick)}'s favourite source</strong>, cited in {f['answers']} "
                   f"of its {be[pick]['answers']} answers. {others}.")
    silent = max(eng, key=lambda e: be[e]["pct_no_sources"])
    if be[silent]["pct_no_sources"] >= 10:
        out.append(f"<strong>{label(silent)} answers {be[silent]['pct_no_sources']:g}% of questions without "
                   "citing a single site</strong>, from what the model already knows.")
    out.append(f"<strong>The engines rarely agree</strong>: all three cite a common site for only "
               f"{agg['pct_engines_share_a_source']:g}% of the questions they all sourced.")
    return out


def render(agg: dict, base_url: str) -> tuple[str, str, str]:
    """(title, body_html, head_extra) for the research page."""
    esc = html.escape
    month = datetime.strptime(agg["month"], "%Y-%m").strftime("%B %Y")
    nxt = next_edition(agg["month"])
    o, be, eng = agg["overall"], agg["by_engine"], agg["engines"]
    label = lambda e: ENGINE_LABELS.get(e, e)  # noqa: E731
    dot = lambda e: f"<span class='rs-dot' style='--c:{ENGINE_COLORS.get(e, '#888')}'></span>"  # noqa: E731

    top = o["top_domains"][0] if o["top_domains"] else {"domain": "-", "answers": 0}
    tiles = [
        (f"{o['answers']}", "AI answers measured", f"{agg['keywords']} questions, 3 engines"),
        (f"{o['community_pct']:g}%", "of cited sites are community sites", "Reddit, YouTube, Quora and similar"),
        (f"{agg['pct_engines_share_a_source']:g}%", "of questions where all three agree", "on at least one source"),
        (esc(top["domain"]), "cited most across all engines", f"in {top['answers']} answers"),
    ]
    # The last tile holds a domain, not a number: smaller type so it never breaks mid-name.
    kpis = "".join(f"<div class='rs-kpi'><div class='rs-kpi-val{' text' if i == 3 else ''}'>{v}</div>"
                   f"<div class='rs-kpi-lbl'>{l}</div><div class='rs-kpi-sub'>{s}</div></div>"
                   for i, (v, l, s) in enumerate(tiles))

    findings = "".join(f"<li>{f}</li>" for f in _findings(agg))

    metrics = [("Sites cited per answer", "avg_sources", "", "when any are cited"),
               ("Community share", "community_pct", "%", "of the sites each engine cites"),
               ("Answers with no sources", "pct_no_sources", "%", "answered from memory"),
               ("Answers with a table", "pct_table", "%", "of all answers")]
    cards = ""
    for title, key, unit, sub in metrics:
        vmax = max(be[e][key] for e in eng) or 1
        rows = "".join(
            f"<div class='rs-bar-row'><span class='rs-bar-name'>{dot(e)}{label(e)}</span>"
            f"<span class='rs-bar-track'><span class='rs-bar' style='width:{max(be[e][key] / vmax * 100, 1.5):.1f}%;"
            f"--c:{ENGINE_COLORS.get(e, '#888')}'></span></span>"
            f"<span class='rs-bar-val'>{be[e][key]:g}{unit}</span></div>" for e in eng)
        cards += f"<div class='rs-card'><h3>{title}</h3><p class='rs-card-sub'>{sub}</p>{rows}</div>"

    smax = max((t["answers"] for e in eng for t in be[e]["top_domains"][:8]), default=1)
    site_cols = ""
    for e in eng:
        rows = "".join(
            f"<li><span class='rs-site'><span class='rs-site-name' title='{esc(t['domain'], quote=True)}'>"
            f"{esc(t['domain'])}</span>"
            + ("<span class='rs-tag'>community</span>" if dfs.is_community(t["domain"]) else "") + "</span>"
            f"<span class='rs-bar-track'><span class='rs-bar' style='width:{t['answers'] / smax * 100:.1f}%;"
            f"--c:{ENGINE_COLORS.get(e, '#888')}'></span></span><span class='rs-bar-val'>{t['answers']}</span></li>"
            for t in be[e]["top_domains"][:8])
        site_cols += (f"<div class='rs-card'><h3>{dot(e)}{label(e)}</h3>"
                      f"<p class='rs-card-sub'>{be[e]['answers']} answers</p><ol class='rs-sites'>{rows}</ol></div>")

    dots = "".join(
        f"<span class='rs-q{' on' if a['shared'] else ''}' title='{esc(a['keyword'], quote=True)}"
        + (f": all three cite {esc(', '.join(a['shared'][:2]), quote=True)}" if a["shared"] else "") + "'></span>"
        for a in sorted(agg["agreement"], key=lambda a: (not a["shared"], a["category"], a["keyword"])))
    n_agree = sum(1 for a in agg["agreement"] if a["shared"])

    cmax = max((v for row in agg["by_category_engine"].values() for v in row.values()), default=1) or 1
    cats = sorted(agg["by_category"].items(), key=lambda kv: -kv[1]["community_pct"])
    heat_rows = "".join(
        f"<tr><th scope='row'>{esc(c)}</th>"
        + "".join(f"<td class='rs-heat' style='--a:{agg['by_category_engine'][c][e] / cmax:.2f}'>"
                  f"{agg['by_category_engine'][c][e]:g}%</td>" for e in eng)
        + f"<td>{esc(s['top_domains'][0]['domain']) if s['top_domains'] else '-'}</td></tr>"
        for c, s in cats)
    heat = ("<div class='rs-scroll'><table class='rs-table'><caption class='sr'>Community share of cited sites, "
            "by topic and engine</caption><thead><tr><th scope='col'>Topic</th>"
            + "".join(f"<th scope='col'>{dot(e)}{label(e)}</th>" for e in eng)
            + "<th scope='col'>Most cited site</th></tr></thead><tbody>" + heat_rows + "</tbody></table></div>")

    legend = "".join(f"<span class='rs-legend-item'>{dot(e)}{label(e)}</span>" for e in eng)
    headline = (f"Across {o['answers']} AI answers to {agg['keywords']} everyday questions in {month}, "
                f"{o['community_pct']:g}% of the sites cited were community sites such as Reddit, YouTube "
                f"and Quora, and the three engines cited at least one common site for only "
                f"{agg['pct_engines_share_a_source']:g}% of the questions.")

    body = f"""<header class="rs-hero">
<p class="rs-eyebrow">Vantage research &middot; {esc(month)}</p>
<h1>What AI answer engines cite</h1>
<p class="rs-lede">We ask ChatGPT, Gemini and Perplexity the same {agg['keywords']} everyday questions every month
and record which websites their answers cite. Here is what {esc(month)} looked like.</p>
<p class="rs-badges"><span class="rs-badge">Updated monthly</span><span class="rs-badge">Next edition {esc(nxt)}</span>
<a class="rs-badge rs-badge-link" href="{base_url}/research/data.json">Download the data (JSON)</a></p>
</header>

<section class="rs-kpis" aria-label="Headline numbers">{kpis}</section>

<section class="rs-section"><h2>What stood out</h2><ul class="rs-findings">{findings}</ul></section>

<section class="rs-section"><h2>How the engines compare</h2>
<p class="rs-legend">{legend}</p>
<div class="rs-grid rs-grid-2">{cards}</div>
<p class="rs-note">Each bar is scaled to the highest value in its own panel; the number beside it is the actual value.</p></section>

<section class="rs-section"><h2>Where each engine gets its answers</h2>
<p class="rs-card-sub">The sites each engine cites most, by the number of answers citing them. All three panels share one scale.</p>
<div class="rs-grid rs-grid-3">{site_cols}</div></section>

<section class="rs-section"><h2>Do the engines agree?</h2>
<p>Each dot is one question that all three engines answered with sources. A filled dot means all three cited at
least one of the same sites: <strong>{n_agree} of {agg['keywords_compared']}</strong>. Hover a dot to see the question.</p>
<div class="rs-dots" role="img" aria-label="{n_agree} of {agg['keywords_compared']} questions had a source cited by all three engines">{dots}</div>
<p class="rs-legend"><span class="rs-legend-item"><span class="rs-q on"></span>all three share a source</span>
<span class="rs-legend-item"><span class="rs-q"></span>no site in common</span></p></section>

<section class="rs-section"><h2>Community share by topic</h2>
<p class="rs-card-sub">Share of each engine's cited sites that are community sites, per topic. Darker is higher.</p>
{heat}</section>

<section class="rs-section rs-cta">
<h2>How does your site do?</h2>
<p>Run the same checks on your own domain from your AI agent: which questions cite you, in ChatGPT, Gemini and
Perplexity, and what changed since last time.</p>
<p class="rs-cta-btns"><a class="btn btn-primary" href="{base_url}/docs/">See the tools</a>
<a class="btn btn-ghost" href="{base_url}/check">Try one keyword free</a></p></section>

<details class="rs-method"><summary>How this is measured</summary>
<p>Each question is asked once per engine, in English, as from the United States. ChatGPT and Gemini are read the
way a person sees them in those apps; Perplexity through its own sonar API with web search on. For every answer we
keep only measurements: which sites it cites, whether it opens with a list, whether it has a table, and how long its
opening is. Never the answer text. "Community sites" are sites whose content is posted by their users (Reddit,
YouTube, X, Quora, LinkedIn, Medium, review sites and similar). A site counts once per answer however many of its
pages are cited. Answers change from run to run, so treat single rows as a snapshot and the totals as the finding.
{o['failed']} of {o['answers'] + o['failed']} requests failed and are left out. Measured on
{esc(agg['generated_at'][:10])}; the same questions run again on {esc(nxt)}.</p></details>"""

    dataset = {
        "@context": "https://schema.org", "@type": "Dataset",
        "name": f"What AI answer engines cite, {month}",
        "description": headline,
        "url": f"{base_url}/research",
        "temporalCoverage": agg["month"],
        "dateModified": agg["generated_at"][:10],
        "creator": {"@type": "Organization", "name": "Vantage", "url": base_url},
        "distribution": [{"@type": "DataDownload", "encodingFormat": "application/json",
                          "contentUrl": f"{base_url}/research/data.json"}],
        "variableMeasured": ["community share of cited sites", "sites cited per answer",
                             "answers citing no sources", "answers with a table", "cross-engine source agreement"],
    }
    head = (f'<meta name="description" content="{esc(headline[:155], quote=True)}">\n'
            f'<link rel="canonical" href="{base_url}/research">\n'
            f'<script type="application/ld+json">{json.dumps(dataset)}</script>')
    return f"What AI answer engines cite, {month}", body, head


RESEARCH_CSS = """
main.check-main > .wrap { max-width: 1060px; }
.rs-hero { padding: 1rem 0 2rem; }
.rs-eyebrow { font-family: var(--font-mono); font-size: 0.78rem; letter-spacing: 0.14em; text-transform: uppercase;
  color: var(--primary); margin: 0 0 0.9rem; }
.check-main .rs-hero h1 { font-size: clamp(2.2rem, 4.5vw + 1rem, 3.6rem); line-height: 1.04; letter-spacing: -0.02em; margin: 0 0 1rem; }
.rs-lede { font-size: 1.15rem; line-height: 1.6; color: var(--ink-2); max-width: 44rem; margin: 0 0 1.4rem; }
.rs-badges { display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 0; }
.rs-badge { font-family: var(--font-mono); font-size: 0.78rem; padding: 0.35rem 0.7rem; border-radius: 999px;
  border: 1px solid var(--line); color: var(--muted); background: var(--surface); }
.rs-badge-link { color: var(--primary); text-decoration: none; border-color: color-mix(in oklch, var(--primary) 45%, transparent); }
.rs-badge-link:hover, .rs-badge-link:focus-visible { background: var(--primary-tint); }
.rs-kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1px; background: var(--line);
  border: 1px solid var(--line); border-radius: 14px; overflow: hidden; margin: 0 0 3rem; }
.rs-kpi { background: var(--surface); padding: 1.4rem 1.2rem; }
.rs-kpi-val { font-size: clamp(1.6rem, 2.4vw + 0.6rem, 2.5rem); font-weight: 600; letter-spacing: -0.02em;
  color: var(--ink); line-height: 1.1; overflow-wrap: anywhere; font-variant-numeric: tabular-nums; }
.rs-kpi-lbl { color: var(--ink-2); margin-top: 0.45rem; line-height: 1.35; }
.rs-kpi-sub { color: var(--muted); font-size: 0.85rem; margin-top: 0.25rem; }
.rs-section { margin: 0 0 3.2rem; }
.check-main .rs-section h2 { font-size: clamp(1.4rem, 1.6vw + 1rem, 1.9rem); margin: 0 0 0.9rem; }
.rs-findings { list-style: none; padding: 0; margin: 0; display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.9rem; }
.rs-findings li { background: var(--surface); border: 1px solid var(--line); border-left: 3px solid var(--primary);
  border-radius: 10px; padding: 1rem 1.1rem; line-height: 1.55; color: var(--ink-2); }
.rs-findings strong { color: var(--ink); }
.rs-grid { display: grid; gap: 1rem; }
.rs-grid-2 { grid-template-columns: repeat(2, 1fr); }
.rs-grid-3 { grid-template-columns: repeat(3, 1fr); }
.rs-card { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 1.1rem 1.2rem; min-width: 0; }
.check-main .rs-card h3 { font-size: 1.02rem; margin: 0; display: flex; align-items: center; gap: 0.5rem; }
.rs-card-sub { color: var(--muted); font-size: 0.9rem; margin: 0.25rem 0 0.9rem; }
.rs-dot { width: 0.7rem; height: 0.7rem; border-radius: 50%; background: var(--c); display: inline-block; flex: none; }
.rs-bar-row, .rs-sites li { display: grid; grid-template-columns: 7.5rem 1fr 3.2rem; align-items: center; gap: 0.6rem;
  padding: 0.32rem 0; }
.rs-bar-name { display: flex; align-items: center; gap: 0.45rem; color: var(--ink-2); font-size: 0.92rem; }
.rs-bar-track { height: 0.62rem; background: var(--surface-2); border-radius: 4px; overflow: hidden; }
.rs-bar { display: block; height: 100%; background: var(--c); border-radius: 0 4px 4px 0; }
.rs-bar-val { text-align: right; font-variant-numeric: tabular-nums; color: var(--ink); font-size: 0.92rem; }
.rs-sites { list-style: none; margin: 0; padding: 0; }
.rs-sites li { grid-template-columns: 1fr 2.4rem; row-gap: 0.3rem; padding: 0.4rem 0; }
.rs-sites .rs-site { grid-column: 1 / -1; }
.rs-site { font-size: 0.92rem; color: var(--ink-2); line-height: 1.3; display: flex; align-items: center;
  gap: 0.4rem; min-width: 0; }
.rs-site-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rs-tag { flex: none; font-size: 0.68rem; color: var(--muted); border: 1px solid var(--line);
  border-radius: 999px; padding: 0.05rem 0.45rem; white-space: nowrap; }
.rs-kpi-val.text { font-size: clamp(1.25rem, 1.4vw + 0.6rem, 1.7rem); padding-top: 0.35rem; }
.rs-legend { display: flex; flex-wrap: wrap; gap: 1.1rem; margin: 0 0 1rem; color: var(--ink-2); font-size: 0.92rem; }
.rs-legend-item { display: inline-flex; align-items: center; gap: 0.45rem; }
.rs-note { color: var(--muted); font-size: 0.85rem; margin: 0.8rem 0 0; }
.rs-dots { display: flex; flex-wrap: wrap; gap: 6px; padding: 1.2rem; background: var(--surface);
  border: 1px solid var(--line); border-radius: 12px; margin: 1rem 0; }
.rs-q { width: 14px; height: 14px; border-radius: 50%; border: 1.5px solid var(--line-strong); display: inline-block;
  flex: none; transition: transform .15s ease; }
.rs-q.on { background: var(--primary); border-color: var(--primary); }
.rs-dots .rs-q:hover { transform: scale(1.5); }
.rs-scroll { overflow-x: auto; }
.rs-table { width: 100%; border-collapse: separate; border-spacing: 3px; font-size: 0.95rem; }
.rs-table th, .rs-table td { padding: 0.55rem 0.6rem; text-align: left; }
.rs-table thead th { color: var(--muted); font-weight: 500; white-space: nowrap; }
.rs-table thead th .rs-dot { margin-right: 0.35rem; }
.rs-table tbody th { color: var(--ink); text-transform: capitalize; font-weight: 500; }
.rs-table td { color: var(--ink-2); }
.rs-heat { text-align: center !important; font-variant-numeric: tabular-nums; border-radius: 6px; color: var(--ink) !important;
  background: color-mix(in oklch, var(--primary) calc(var(--a) * 70%), var(--surface-2)); }
.rs-cta { background: linear-gradient(135deg, var(--primary-tint), transparent 70%); border: 1px solid
  color-mix(in oklch, var(--primary) 40%, transparent); border-radius: 16px; padding: 1.8rem 1.6rem; }
.rs-cta p { color: var(--ink-2); max-width: 40rem; }
.rs-cta-btns { display: flex; flex-wrap: wrap; gap: 0.7rem; margin: 1.2rem 0 0; }
.rs-method { border-top: 1px solid var(--line); padding: 1.2rem 0 0; margin: 0 0 1rem; color: var(--muted); }
.rs-method summary { cursor: pointer; color: var(--ink-2); font-weight: 500; }
.rs-method p { margin-top: 0.8rem; line-height: 1.65; }
.sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
@media (max-width: 860px) { .rs-kpis { grid-template-columns: repeat(2, 1fr); } .rs-grid-3 { grid-template-columns: 1fr; } }
@media (max-width: 640px) { .rs-grid-2, .rs-findings { grid-template-columns: 1fr; }
  .rs-bar-row { grid-template-columns: 6.2rem 1fr 3rem; } .rs-table { font-size: 0.85rem; } }
@media (prefers-reduced-motion: reduce) { .rs-q { transition: none; } }
"""


if __name__ == "__main__":
    import sys
    print(run_month(sys.argv[1], sys.argv[2]))
