"""Shared DataForSEO client for AI-answer-engine visibility checks.

Domain/keyword/platform are call-time parameters rather than baked-in
constants, since this serves arbitrary callers, not one fixed site.

Auth: DATAFORSEO_USERNAME / DATAFORSEO_PASSWORD from env.
"""

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.dataforseo.com/v3"

# Market every check runs in unless the caller picks another. The live ChatGPT
# answer (citation_structure) works in many countries and languages; the
# llm_mentions family (citation_leaders, citation_trend) only has ChatGPT data
# for the United States in English, which server.py enforces before calling.
DEFAULT_COUNTRY = "United States"
DEFAULT_LANGUAGE = "en"

# Sites whose content is posted by their users rather than written by the site.
# Answer engines lean on these heavily (the ZeroRank data, Niche Pursuits
# podcast 2026-09-23, puts them at most of the weight), and they are the
# off-site work a customer can actually go and do. Matched on the registrable
# domain or any subdomain of it (old.reddit.com, m.youtube.com). Only this one
# category is claimed: telling an editorial site from a niche blog or a brand's
# own site needs data we do not have, so everything else is "other".
COMMUNITY_DOMAINS = (
    "reddit.com", "youtube.com", "youtu.be", "x.com", "twitter.com", "quora.com",
    "linkedin.com", "facebook.com", "instagram.com", "tiktok.com", "medium.com",
    "substack.com", "stackoverflow.com", "stackexchange.com", "tripadvisor.com",
    "trustpilot.com", "g2.com", "capterra.com",
)


def is_community(domain: str) -> bool:
    d = (domain or "").strip().lower().removeprefix("www.")
    return any(d == c or d.endswith("." + c) for c in COMMUNITY_DOMAINS)


def source_mix(domains: list[str], weights: list[int] | None = None) -> dict:
    """How much of a set of cited domains is community sites, as a share of
    the domains (or of `weights`, e.g. mention counts, when given)."""
    weights = weights or [1] * len(domains)
    total = sum(weights)
    community_weight = sum(w for d, w in zip(domains, weights) if is_community(d))
    return {
        "community_pct": round(100 * community_weight / total, 1) if total else 0.0,
        "community_domains": [d for d in domains if is_community(d)],
        "other_domains": [d for d in domains if not is_community(d)],
    }


class DataForSEOError(RuntimeError):
    pass


def _auth_header() -> str:
    user = os.environ.get("DATAFORSEO_USERNAME")
    password = os.environ.get("DATAFORSEO_PASSWORD")
    if not user or not password:
        raise DataForSEOError("DATAFORSEO_USERNAME / DATAFORSEO_PASSWORD not set in environment")
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


# After one call times out, the provider is hung, not slow: refuse further calls
# for this long so a caller waits 130s once, not once per keyword (a 10-keyword
# batch used to stack to 21 minutes). Only a timeout trips it, never a 4xx/5xx.
_FAIL_FAST_S = 60
_breaker = {"until": 0.0}


def _call(path: str, payload: list, timeout: int = 130) -> dict:
    if time.monotonic() < _breaker["until"]:
        raise DataForSEOError("provider timed out a moment ago, not retrying yet")
    req = urllib.request.Request(
        f"{API}/{path}",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Authorization": _auth_header(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise DataForSEOError(f"HTTP {e.code}: {e.read().decode()[:300]}") from e
    except Exception as e:  # noqa: BLE001 - surface as one error type to callers
        if "timed out" in str(e):
            _breaker["until"] = time.monotonic() + _FAIL_FAST_S
        raise DataForSEOError(str(e)) from e


def read_balance() -> float:
    req = urllib.request.Request(
        f"{API}/appendix/user_data", method="GET", headers={"Authorization": _auth_header()}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
        return float(d["tasks"][0]["result"][0]["money"]["balance"])
    except urllib.error.HTTPError as e:
        raise DataForSEOError(f"HTTP {e.code}: {e.read().decode()[:300]}") from e
    except Exception as e:  # noqa: BLE001 - malformed/empty body, same as _call
        raise DataForSEOError(str(e)) from e


def _first_group_list(d: dict) -> list:
    """Breakdown arrays (platform/location/language) all sum to the same
    total, so return whichever one is present."""
    for key in ("platform", "location", "language"):
        v = d.get(key)
        if v:
            return v
    return []


def citation_leaders(keyword: str, platform: str = "chat_gpt", limit: int = 5,
                     country: str = DEFAULT_COUNTRY, language: str = DEFAULT_LANGUAGE) -> dict:
    """Who dominates AI-answer citations for this keyword/topic, and
    whether the given domain shows up in that list. ~$0.15/call.

    `limit` is the provider's own cap on how many domains come back (max 10),
    echoed in the result as `top_domains_limit` so a caller can tell
    "not in the top N" apart from "not cited anywhere" - those are different
    claims and this endpoint can only ever support the first one."""
    body = [{"target": [{"keyword": keyword}], "items_list_limit": limit, "platform": platform,
             "location_name": country, "language_code": language}]
    res = _call("ai_optimization/llm_mentions/top_domains/live", body)
    try:
        items = res["tasks"][0]["result"][0]["items"]
        leaders = []
        for it in items:
            group = _first_group_list(it)
            leaders.append({"domain": it["key"], "mentions": group[0].get("mentions") if group else None})
        return {"keyword": keyword, "platform": platform, "country": country, "language": language,
                "top_domains": leaders, "top_domains_limit": limit,
                "source_mix": source_mix([l["domain"] for l in leaders],
                                         [l["mentions"] or 0 for l in leaders])}
    except Exception as e:
        # No "top_domains": [] here on purpose. An empty list next to an error
        # was previously indistinguishable from a real, confirmed-empty result -
        # server.py's compare_domain_present was computed over this same empty
        # list either way, which is how a parse failure turned into a false
        # "not cited" answer. An error response now carries no top_domains key
        # at all, so the caller cannot accidentally read it as a real leaderboard.
        return {"keyword": keyword, "platform": platform, "error": str(e)}


def citation_trend(domain: str, platform: str = "chat_gpt",
                   country: str = DEFAULT_COUNTRY, language: str = DEFAULT_LANGUAGE) -> dict:
    """Month-by-month mention counts for a domain since DataForSEO's
    history began (2025-08-01), oldest to newest. Priced at $0/call on
    every real call made verifying this - unlike
    citation_leaders above, which runs ~$0.15/call. A month with no
    tracked mentions comes back with no "metrics" key at all rather than
    zeros - real behavior found calling this live, not assumed from the
    docs - so that gets normalized to an explicit 0 here rather than
    silently dropped."""
    body = [{"target": [{"domain": domain}], "platform": platform,
             "location_name": country, "language_code": language}]
    res = _call("ai_optimization/llm_mentions/historical/live", body)
    try:
        items = res["tasks"][0]["result"][0]["items"]
    except Exception as e:
        return {"domain": domain, "platform": platform, "months": [], "error": str(e)}
    months = [
        {
            "year": it["year"],
            "month": it["month"],
            "mentions": (it.get("metrics") or {}).get("mentions", 0),
            "ai_search_volume": (it.get("metrics") or {}).get("ai_search_volume", 0),
        }
        for it in items
    ]
    months.sort(key=lambda m: (m["year"], m["month"]))
    return {"domain": domain, "platform": platform, "months": months}


_LIST_RE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+", re.MULTILINE)
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)

# Markdown markers, removed before anything is counted or shown. DataForSEO
# returns the answer as markdown, so an opening arrives as
# "## Best free password manager: **Bitwarden**". Counting that with .split()
# scores "##" as a word, which is how a five-word opening was reported as six.
_MD_MARKERS = [
    (re.compile(r"\[([^\]]*)\]\([^)]*$"), ""),
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(r"^#{1,6}\s*", re.MULTILINE), ""),
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),
    (re.compile(r"(?<!\w)_(.+?)_(?!\w)", re.S), r"\1"),
    (re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", re.S), r"\1"),
    (re.compile(r"`([^`]*)`"), r"\1"),
]


# A citation chip in an answer's markdown is a link whose visible text is only
# the domain it points at, e.g. "([nhs.uk](https://www.nhs.uk/...))". That is
# the answer citing a source, not saying anything, so chips are removed before
# links become text. ChatGPT also sends them bare and back to back
# ("options: [appvulture.com](...)[alternativeto.net](...)"), which used to
# reach openings and outlines as "appvulture.comalternativeto.net" (2026-10-01).
# The link text must match the URL's host, so "[Node.js](https://nodejs.org)"
# stays a word.
_CHIP = r"\[\s*[\w.-]+\.[a-z]{2,}\s*\]\([^)\s]*\)"
_CHIP_PARTS_RE = re.compile(r"\[\s*([\w.-]+\.[a-z]{2,})\s*\]\(([^)\s]*)\)", re.IGNORECASE)
_CHIP_RUN_RE = re.compile(rf"\(\s*(?:{_CHIP}\s*)+\)|(?:{_CHIP}[ \t]*)+", re.IGNORECASE)


def _drop_chip_run(m: re.Match) -> str:
    for chip in _CHIP_PARTS_RE.finditer(m.group(0)):
        text = chip.group(1).lower().removeprefix("www.")
        host = (urllib.parse.urlsplit(chip.group(2)).hostname or "").lower()
        if host != text and not host.endswith("." + text):
            return m.group(0)
    return " "


def strip_markdown(text: str) -> str:
    out = _CHIP_RUN_RE.sub(_drop_chip_run, text or "")
    for pattern, repl in _MD_MARKERS:
        out = pattern.sub(repl, out)
    return " ".join(out.split())


def _parse_opening(markdown: str) -> dict:
    """Shared structural read of a markdown document's opening, used
    both for the AI-cited winning answer (citation_structure) and for
    a caller's own page (page_structure), so the two are computed the
    exact same way and stay directly comparable. A list or a table ends the
    opening: a page that leads with a comparison table was reported as a
    176-word opening made of its table cells (2026-10-01)."""
    starts = [m.start() for m in (_LIST_RE.search(markdown), _TABLE_ROW_RE.search(markdown)) if m]
    cutoff = min(starts) if starts else len(markdown)
    para_end = markdown.find("\n\n")
    if para_end != -1 and para_end < cutoff:
        cutoff = para_end
    opening = strip_markdown(markdown[:cutoff])
    return {
        # The opening itself, not only facts about it. Reporting "opens with a
        # sentence, 11 words" while withholding the sentence leaves the reader
        # with nothing to act on and no way to check the claim.
        "opening": opening,
        "leads_with_list": bool(_LIST_RE.match(markdown.lstrip())),
        "opening_word_count": len(opening.split()),
        "opening_has_number": bool(re.search(r"\d", opening)),
    }


def mentions_any(markdown: str, terms: list[str]) -> bool:
    """True if the answer's own text names any of `terms` (whole word, case
    insensitive), ignoring domain-only citation links (strip_markdown drops
    them). "Named" is a different claim from "cited": an answer can recommend
    a product without linking it, and can link a page it never names. Terms
    under 3 characters are skipped, since they match inside ordinary words."""
    text = strip_markdown(markdown)
    for term in terms:
        term = (term or "").strip()
        if len(term) < 3:
            continue
        if re.search(r"(?<![\w-])" + re.escape(term) + r"(?![\w-])", text, re.IGNORECASE):
            return True
    return False


# Words a title-style first line can add without saying anything beyond the
# question itself ("What is coherent breathing?", "Box breathing explained").
_RESTATE_FILLER = {"what", "is", "are", "how", "to", "do", "does", "why", "the", "a", "an",
                   "of", "for", "and", "in", "on", "with", "your", "you", "it", "its",
                   "guide", "explained", "overview", "definition"}
_WORD_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _norm_words(text: str) -> set[str]:
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in _WORD_RE.findall(text.lower())}


def _drop_restated_heading(markdown: str, keyword: str) -> str:
    """Drop the answer's first line when it only restates the question.

    ChatGPT often opens with a title such as "What is coherent breathing?" or
    "5-4-3-2-1 Grounding Technique" and gets to the point in the paragraph
    after it. Counting that title as the opening reported "the winning answer
    gets to the point in 3 words" (found 2026-09-12). A first line whose words
    are all the keyword's own plus filler is dropped; one that carries anything
    more ("Best free password manager: **Bitwarden**") is the answer and stays.
    Falls back to the markdown unchanged if nothing would be left."""
    stripped = markdown.lstrip()
    first, _, rest = stripped.partition("\n")
    words = _norm_words(strip_markdown(first))
    kw_words = _norm_words(keyword)
    if not rest.strip() or not words or not (words & kw_words):
        return markdown
    if words <= kw_words | _RESTATE_FILLER:
        return rest.lstrip("\n")
    return markdown


_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_TOP_LIST_ITEM_RE = re.compile(r"^(?:[-*]|\d+\.)\s+(.+)$", re.MULTILINE)
_NUMBERING_RE = re.compile(r"^(?:step\s+)?\d+[.):]\s*", re.IGNORECASE)
# An emoji may come before the bold head ("😌 **Stress relief:** Oak or ...").
_BOLD_LEAD_RE = re.compile(r"^[^\w*]*\*\*(.+?)\*\*")
# A markdown table's separator row ("--- | ---", "|:---|---:|"). The provider
# also collapses a page's separator to a single "|---|" whatever the column
# count (2026-10-01), so one pipe-bounded cell counts too; a bare "---" is a
# horizontal rule, not a table.
_TABLE_SEP_RE = re.compile(
    r"^\s*(?:\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?|\|\s*:?-{3,}:?\s*\|)\s*$", re.MULTILINE)
_OUTLINE_MAX = 12
_OUTLINE_ITEM_WORDS = 12


def _outline(markdown: str) -> list[str]:
    """A document's section heads, in order: its headings, or its top-level
    list items when it has fewer than two headings. Numbering and markdown are
    removed, each head is cut to _OUTLINE_ITEM_WORDS words and repeats are
    dropped: the provider has returned an answer with its last section twice
    (2026-09-23, "7. Measure the right numbers"). Heads only, never the text
    under them, so this describes someone else's answer without copying it."""
    heads = _HEADING_RE.findall(markdown)
    if len(heads) < 2:
        heads = []
        for item in _TOP_LIST_ITEM_RE.findall(markdown):
            bold = _BOLD_LEAD_RE.match(item.strip())
            heads.append(bold.group(1) if bold else re.split(r":\s| - | = ", item, maxsplit=1)[0])
    out, seen = [], set()
    for head in heads:
        text = _NUMBERING_RE.sub("", strip_markdown(head)).strip(" :-")
        words = text.split()
        if not words:
            continue
        if len(words) > _OUTLINE_ITEM_WORDS:
            text = " ".join(words[:_OUTLINE_ITEM_WORDS]) + "..."
        if text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(text)
        if len(out) == _OUTLINE_MAX:
            break
    return out


def _has_table(markdown: str) -> bool:
    return bool(_TABLE_SEP_RE.search(markdown))


# Answer engines a live-answer check can read. chat_gpt and gemini are
# DataForSEO's scrapers of the consumer apps (what a person sees); perplexity
# is Perplexity's own sonar API with web search on, the closest thing to its
# app that the provider offers. All three measured at ~$0.004-0.006 a call
# on 2026-09-24, so all cost 1 unit.
ENGINES = ("chat_gpt", "gemini", "perplexity")

# Perplexity localises by ISO country code, not by name. Common markets by
# name; any 2-letter code is also accepted as is.
_ISO_COUNTRIES = {
    "united states": "US", "united kingdom": "GB", "canada": "CA", "australia": "AU",
    "new zealand": "NZ", "ireland": "IE", "germany": "DE", "france": "FR", "italy": "IT",
    "spain": "ES", "portugal": "PT", "netherlands": "NL", "belgium": "BE", "switzerland": "CH",
    "austria": "AT", "sweden": "SE", "norway": "NO", "denmark": "DK", "finland": "FI",
    "poland": "PL", "brazil": "BR", "mexico": "MX", "argentina": "AR", "india": "IN",
    "japan": "JP", "singapore": "SG", "south africa": "ZA", "united arab emirates": "AE",
}


def country_iso(country: str) -> str | None:
    c = (country or "").strip()
    if len(c) == 2 and c.isalpha():
        return c.upper()
    return _ISO_COUNTRIES.get(c.lower())


def _domains_from(sources: list[dict]) -> list[str]:
    """Distinct cited domains in first-seen order. The provider can list the
    same domain twice (two pages on bitwarden.com cited separately), which
    inflated num_sources_cited: a "9 sources" answer with 2 duplicates is
    really 7 sites. Deduped before counting or truncating, so the count and
    the list it describes always agree."""
    domains = []
    for src in sources:
        d = src.get("domain") or urllib.parse.urlsplit(src.get("url") or "").hostname
        if d and d not in domains:
            domains.append(d)
    return domains


MAX_BRANDS = 15

# Brand names are read from where an answer puts the things it recommends: the
# first column of a table, a heading ("### 2. Attio"), and a bold name at the
# start of a list item or line ("- **Zoho CRM** - for..."). The provider's own
# `brand_entities` field was null on every live answer checked on 2026-09-29,
# although its docs describe it, so it is not used.
# Headings, table separators and numbering reuse the outline's own patterns
# (_HEADING_RE, _TABLE_SEP_RE, _NUMBERING_RE above).
_BRAND_ITEM_RE = re.compile(r"^(?:[-*+]\s+|\d+[.)]\s+)?\*\*([^*]{1,60})\*\*")
_BRAND_CUT_RE = re.compile(r"\s+[-–—|/]\s+|[:(–—]")
_FEATURE_HEADERS = {"", "feature", "features", "category", "aspect", "criteria", "area", "factor", "metric",
                    "comparison", "dimension", "use case", "need", "if you need", "what you need"}
# First words of labels and section titles, never of a product's name.
_NOT_A_NAME = set("""best why trade pros cons price pricing bottom verdict overall summary recommendation
recommended recommendations my the a an for if what how when which who key note notes tip tips option options
alternative alternatives top quick final conclusion comparison feature features choose choosing one two three
important other others also honorable budget free paid pick picks shortlist winner use used good great strong
it this these that those don't do does step steps example table tool tools platform platforms category criteria
cost costs plan plans trial ideal main your you we our consider considerations here there bonus runner
runners standout standouts caveat caveats limitations limitation drawbacks drawback strengths strength
weaknesses weakness in on at by with without and or but not no yes all most more less simple simplest
easiest cheapest fastest technical local content keyword keywords backlinks backlink rank marketing
enterprise small large startups startup agencies agency teams team beginners advanced basic integrated
industry broader where trade-off trade-offs downside downsides upside notable watch starting integrations
integration support setup learning ease security scalability customization automation reporting value
target audience verdict: ideal limits availability performance reliability onboarding""".split())
# Short words a multi-word product name can carry in lower case.
_NAME_CONNECTORS = {"of", "for", "and", "&", "by", "the", "de", "/", "+", "x"}
# Last words of the criteria an answer bolds ("**Engine Coverage:**",
# "**Tracking Method:**"), never of a product's name.
_CRITERIA_ENDINGS = set("""coverage method methods insights insight features pricing tracking reporting data
accuracy value cost costs speed quality usability flexibility compliance depth breadth frequency sources
metrics capabilities approach strategy goals needs size fit experience interface workflow workflows
customization considerations factors criteria requirements use cases""".split())
_ACRONYM_COMBO_RE = re.compile(r"^[A-Z]{2,5}(?:\s*[+/&]\s*[A-Z]{2,5})+$")


def _clean_name(text: str) -> str:
    t = _NUMBERING_RE.sub("", strip_markdown(text).lstrip("#").strip())
    t = _BRAND_CUT_RE.split(t, maxsplit=1)[0]
    return " ".join(t.strip(" .,*_").split())


def _looks_like_name(name: str, labels: set[str]) -> bool:
    words = name.split()
    if not 1 <= len(words) <= 5 or len(name) > 40 or name.endswith(":"):
        return False
    if words[0].lower().strip("'’") in _NOT_A_NAME or re.fullmatch(r"[\d\s.,%$]+", name):
        return False
    if re.search(r"\b(vs|versus)\b|[?!]", name, re.I) or _ACRONYM_COMBO_RE.match(name):
        return False
    if len(words) > 1 and words[-1].lower() in _CRITERIA_ENDINGS:
        return False
    # "Help Scout", "monday CRM", "Jira Service Management": after the first
    # word, a product name capitalises its words. "Where Ahrefs stands out"
    # and "Rank tracking" are section labels.
    if any(w[0].islower() and w.lower() not in _NAME_CONNECTORS for w in words[1:]):
        return False
    # A product name has a capital letter somewhere ("monday CRM"), or matches
    # a site the answer cites ("folk" and folk.app).
    return bool(re.search(r"[A-Z]", name)) or re.sub(r"[^a-z0-9]", "", name.lower()) in labels


def extract_brands(markdown: str, domains: list[str] | None = None) -> list[str]:
    """Names an answer recommends, first-seen order, deduped by case. Names
    only: a measurement, like source_domains, never answer text. Never
    raises, since the monthly research run fails a whole row on an exception."""
    try:
        return _extract_brands(markdown, domains)
    except Exception:  # noqa: BLE001 - every live tool calls this; it may only ever return fewer names
        return []


def _extract_brands(markdown: str, domains: list[str] | None) -> list[str]:
    labels = {re.sub(r"[^a-z0-9]", "", d.lower().removeprefix("www.").split(".")[0]) for d in domains or []}
    found: list[str] = []
    lines = (markdown or "").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("|") and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1].strip()):
            header = [_clean_name(c) for c in line.strip("|").split("|")]
            j, body = i + 2, []
            while j < len(lines) and lines[j].strip().startswith("|"):
                body.append(lines[j].strip().strip("|").split("|"))
                j += 1
            if header and header[0].lower() in _FEATURE_HEADERS:
                found += header[1:]  # brands across the top, features down the side
            else:
                found += [_clean_name(r[0]) for r in body if r]
            i = j
            continue
        m = _HEADING_RE.match(line)
        if m:
            found.append(_clean_name(m.group(1)))
        else:
            # "**Zoho CRM** - ..." and Gemini's "**Semrush (AI Visibility
            # Toolkit):** ...". Labels such as "**Best For:**" fall to the
            # first-word check in _looks_like_name.
            m = _BRAND_ITEM_RE.match(line)
            if m:
                found.append(_clean_name(m.group(1)))
        i += 1
    out: list[str] = []
    for name in found:
        if not _looks_like_name(name, labels):
            continue
        # "Otterly" after "Otterly AI" (or the other way round) is the same
        # product named twice: keep the first spelling.
        low = name.lower().split()
        if any(low == n.lower().split()[:len(low)] or n.lower().split() == low[:len(n.split())] for n in out):
            continue
        out.append(name)
    return out[:MAX_BRANDS]


def fetch_answer(keyword: str, engine: str = "chat_gpt",
                 country: str = DEFAULT_COUNTRY, language: str = DEFAULT_LANGUAGE) -> dict:
    """One live answer from `engine`, normalised to {"markdown", "domains",
    "brands", "model", "checked_at"} or {"error"}. Raises DataForSEOError on
    transport failure, like every other call here."""
    if engine == "perplexity":
        iso = country_iso(country)
        if not iso:
            return {"error": f'Perplexity needs a country it can localise to; "{country}" is not one '
                             'Vantage knows. Pass a 2-letter country code, e.g. "IT".'}
        body = [{"user_prompt": keyword[:500], "model_name": "sonar", "max_output_tokens": 1200,
                 "web_search_country_iso_code": iso}]
        path = "ai_optimization/perplexity/llm_responses/live"
    elif engine == "gemini":
        body = [{"keyword": keyword, "language_code": language, "location_name": country}]
        path = "ai_optimization/gemini/llm_scraper/live/advanced"
    else:
        body = [{"keyword": keyword, "language_code": language, "location_name": country,
                 "force_web_search": True}]
        path = "ai_optimization/chat_gpt/llm_scraper/live/advanced"
    res = _call(path, body, timeout=130)
    # The provider can reply with no task at all ("tasks": null) when the
    # request fails as a whole, e.g. a rate or account limit. Found 2026-09-24
    # when it crashed the first research run 13 minutes in; before 1.8.0 this
    # parse sat inside citation_structure's try, after it, it raised through
    # every live tool. Treated as transient: the caller retries or refunds.
    tasks = (res or {}).get("tasks") or []
    if not tasks:
        return {"error": (res or {}).get("status_message") or "provider returned no task", "transient": True}
    task = tasks[0]
    if task.get("status_code") != 20000:
        return {"error": task.get("status_message")}
    # The provider can answer status 20000 with "result": null (no answer
    # produced for this keyword). Say so instead of a NoneType TypeError.
    if not task.get("result"):
        return {"error": "provider returned no answer for this keyword (empty result)"}
    result = task["result"][0]
    if engine == "perplexity":
        sections = [s for it in result.get("items") or [] for s in it.get("sections") or []]
        markdown = "\n\n".join(s.get("text") or "" for s in sections)
        sources = [a for s in sections for a in (s.get("annotations") or [])]
        model = result.get("model_name")
    else:
        markdown = result.get("markdown") or ""
        sources = result.get("sources") or []
        model = result.get("model")
    domains = _domains_from(sources)
    return {"markdown": markdown, "domains": domains, "brands": extract_brands(markdown, domains),
            "model": model, "checked_at": result.get("datetime")}


def citation_structure(keyword: str, mention_terms: list[str] | None = None,
                       country: str = DEFAULT_COUNTRY, language: str = DEFAULT_LANGUAGE,
                       engine: str = "chat_gpt", with_brands: bool = False) -> dict:
    """Structural shape of the AI-generated answer actually cited for
    this keyword: does it lead with a list, how long is the opening,
    how many sources does it cite, which domains. ~$0.004/call.
    With `mention_terms`, also reports whether the answer text names any of
    them ("mentioned"), from the same response at no extra cost.
    With `with_brands`, adds "brands": the brand names the answer names
    (ChatGPT and Gemini only; always [] for Perplexity). Off by default so
    the MCP tools' responses keep their documented shape."""
    answer = fetch_answer(keyword, engine=engine, country=country, language=language)
    if answer.get("error"):
        return {"keyword": keyword, "error": answer["error"], **({"transient": True} if answer.get("transient") else {})}
    try:
        markdown = answer["markdown"]
        domains = answer["domains"]
        # Measured from the point on: a first line that only restates the
        # question is dropped first. Mentions still read the whole answer.
        body_md = _drop_restated_heading(markdown, keyword)
        parsed = _parse_opening(body_md)
        # A short look at what follows the opening, so a caller can see the
        # answer continues rather than being told it does. Truncated hard: this
        # is a preview, not a copy of somebody else's answer.
        # Slice the RAW markdown by the raw cutoff, not by the cleaned
        # opening's length: cleaning shortens the string, so using the cleaned
        # length here would re-include the tail of the opening.
        list_match = _LIST_RE.search(body_md)
        cutoff = list_match.start() if list_match else len(body_md)
        para_end = body_md.find("\n\n")
        if para_end != -1 and para_end < cutoff:
            cutoff = para_end
        detail_preview = strip_markdown(body_md[cutoff:])[:240]
        if len(strip_markdown(body_md[cutoff:])) > 240:
            detail_preview = detail_preview.rsplit(" ", 1)[0].rstrip(",;:") + "..."
        return {
            "keyword": keyword,
            "engine": engine,
            "model": answer["model"],
            "checked_at": answer["checked_at"],
            "country": country,
            "language": language,
            **parsed,
            "detail_preview": detail_preview,
            "outline": _outline(body_md),
            "has_table": _has_table(body_md),
            "num_sources_cited": len(domains[:10]),
            "source_domains": domains[:10],
            "source_mix": source_mix(domains[:10]),
            **({"mentioned": mentions_any(markdown, mention_terms)} if mention_terms else {}),
            **({"brands": answer.get("brands") or []} if with_brands else {}),
        }
    except Exception as e:
        return {"keyword": keyword, "error": str(e)}


MAX_SAMPLES = 5


def sample_structures(keyword: str, samples: int, mention_terms: list[str] | None = None,
                      country: str = DEFAULT_COUNTRY, language: str = DEFAULT_LANGUAGE,
                      engine: str = "chat_gpt") -> list[dict]:
    """`samples` independent live answers for one keyword, fetched in parallel
    (one answer takes 10-30s, so five in series would outlast most clients'
    timeouts). Each entry is a citation_structure result or {"error"}; a
    transport failure becomes an error entry instead of sinking the others.
    Answers change from run to run, which is the whole reason to ask twice."""
    from concurrent.futures import ThreadPoolExecutor

    def one(_):
        try:
            return citation_structure(keyword, mention_terms=mention_terms, country=country,
                                      language=language, engine=engine)
        except DataForSEOError as e:
            return {"keyword": keyword, "error": str(e), "transient": True}

    with ThreadPoolExecutor(max_workers=samples) as pool:
        return list(pool.map(one, range(samples)))


def cited_questions(domain: str, platform: str = "chat_gpt", limit: int = 20,
                    country: str = DEFAULT_COUNTRY, language: str = DEFAULT_LANGUAGE) -> dict:
    """Questions whose tracked AI answers cite `domain` as a source, most
    asked first. Starts from the domain, so nobody has to guess keywords
    first. ~$0.12/call at limit 20 (measured 2026-09-24)."""
    body = [{"target": [{"domain": domain, "search_scope": ["sources"], "include_subdomains": True}],
             "platform": platform, "location_name": country, "language_code": language,
             "limit": limit, "order_by": ["ai_search_volume,desc"]}]
    res = _call("ai_optimization/llm_mentions/search/live", body)
    try:
        task = res["tasks"][0]
        if task.get("status_code") != 20000:
            return {"domain": domain, "error": task.get("status_message")}
        result = (task.get("result") or [{}])[0] or {}
        target = domain.lower().removeprefix("www.")
        questions = []
        for it in result.get("items") or []:
            domains = _domains_from(it.get("sources") or [])
            position = next((i for i, d in enumerate(domains, 1)
                             if d.lower().removeprefix("www.") == target
                             or d.lower().endswith("." + target)), None)
            questions.append({
                "question": it.get("question"),
                "ai_search_volume": it.get("ai_search_volume"),
                "your_position": position,
                "source_domains": domains[:10],
                "last_seen": it.get("last_response_at"),
            })
        return {"domain": domain, "platform": platform, "country": country, "language": language,
                "total_questions": result.get("total_count") or 0, "questions": questions}
    except Exception as e:
        return {"domain": domain, "error": str(e)}


# Whole words that make a search a buying question. Whole words matter: a
# bare "app" matched "whatsapp" and filled a CRM's list with WhatsApp how-tos.
BUYER_SEARCH_RE = (r"(^| )(best|top|vs|versus|alternatives?|reviews?|compare|comparison|software|tools?"
                   r"|apps?|platforms?|crm|pricing)( |$)")


def ranked_keywords(domain: str, limit: int = 30, max_position: int = 10,
                    country: str = DEFAULT_COUNTRY, language: str = DEFAULT_LANGUAGE) -> dict:
    """Buying-type Google searches `domain` already ranks for in the top
    `max_position`, most searched first: {"domain", "keywords": [{"keyword",
    "search_volume", "position", "url", "intent"}]} or {"error"}. Buying-type
    means commercial or transactional intent, or a BUYER_SEARCH_RE word, and
    never navigational: unfiltered, a CRM's top searches were JavaScript and
    WhatsApp how-tos its blog happens to rank for. The free site check starts
    here. $0.0152-0.0156 at limit 30, measured live 2026-09-29."""
    body = [{"target": domain, "location_name": country, "language_code": language, "limit": limit,
             "order_by": ["keyword_data.keyword_info.search_volume,desc"],
             "filters": [["ranked_serp_element.serp_item.rank_group", "<=", max_position], "and",
                         ["keyword_data.search_intent_info.main_intent", "<>", "navigational"], "and",
                         [["keyword_data.search_intent_info.main_intent", "in", ["commercial", "transactional"]],
                          "or", ["keyword_data.keyword", "regex", BUYER_SEARCH_RE]]]}]
    res = _call("dataforseo_labs/google/ranked_keywords/live", body, timeout=60)
    try:
        task = res["tasks"][0]
        if task.get("status_code") != 20000:
            return {"domain": domain, "error": task.get("status_message")}
        result = (task.get("result") or [{}])[0] or {}
        out = []
        for it in result.get("items") or []:
            kd = it.get("keyword_data") or {}
            serp = (it.get("ranked_serp_element") or {}).get("serp_item") or {}
            out.append({
                "keyword": kd.get("keyword"),
                "search_volume": (kd.get("keyword_info") or {}).get("search_volume") or 0,
                "position": serp.get("rank_group"),
                "url": serp.get("url"),
                "intent": (kd.get("search_intent_info") or {}).get("main_intent"),
            })
        return {"domain": domain, "keywords": [k for k in out if k["keyword"]]}
    except Exception as e:
        return {"domain": domain, "error": str(e)}


# A web page's markdown does not start where its content does. DataForSEO's
# page_as_markdown puts the H1 first, then whatever the page shows before the
# body: related-article cards, "Getting started" style jump labels, bylines,
# "5 min read". Taking the first block of that as the opening measured the
# title plus a link to another article, and reported "yours is 19 words" for
# an opening nobody wrote (found 2026-09-11 by an internal test). Page chrome
# is skipped line by line until the
# first line of real content: a heading, a line that is only links or images,
# and a short label (under _CHROME_MAX_WORDS words, not ending like a sentence)
# are all chrome. The AI answer is NOT put through this: its first line is the
# answer, headings included, so that side is measured exactly as before.
_LINK_OR_IMAGE_RE = re.compile(r"!?\[[^\]]*\]\([^)]*\)")
_SENTENCE_END_RE = re.compile(r"[.!?:]$")
_CHROME_MAX_WORDS = 5
# A related-article card: the provider writes it as a heading followed by a link
# with the same text. Mid-page cards were reported as the page's own sections
# (links to other articles listed as an article's headings, 2026-10-01).
_CARD_RE = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*\n\s*\[\1\]\([^)]*\)", re.MULTILINE)


def _page_body(markdown: str) -> str:
    """The page's markdown from its first line of real content onward, with
    related-article cards removed and a blank line forced before every heading
    so a paragraph that runs straight into the next heading still ends there.
    Falls back to the whole markdown if every line looks like chrome, rather
    than measuring nothing."""
    markdown = _CARD_RE.sub("", markdown)
    lines = markdown.split("\n")
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        text = _LIST_RE.sub("", line, count=1)
        rest = _LINK_OR_IMAGE_RE.sub("", text).strip(" |-*")
        if not rest:
            continue
        if len(strip_markdown(text).split()) < _CHROME_MAX_WORDS and not _SENTENCE_END_RE.search(rest):
            continue
        body = "\n".join(lines[i:])
        return re.sub(r"\n(#{1,6}\s)", r"\n\n\1", body)
    return markdown


# A markdown link's URL, for counting a page's outbound links. The provider's
# markdown is not always well formed: a reference list came back (2026-09-12) as
# "[Li et al. ...](https://pubmed.ncbi.nl[Nature](https://pubmed.ncbi.nlm.nih.gov/26855425/)gov/26855425/)",
# a second link spliced into the first URL, and "anything up to )" reported
# "pubmed.ncbi.nl[Nature](https:" as a linked domain. So a URL here has no
# whitespace, brackets or angle brackets, and parentheses only as a balanced
# pair (Wikipedia's "Foo_(bar)"); link text may hold one level of brackets
# ("[see [1]](...)"). A link breaking those rules is skipped, not guessed at.
_MD_LINK_URL_RE = re.compile(
    r"\[(?:[^\[\]]|\[[^\[\]]*\])*\]\((https?://(?:[^\s()\[\]<>]|\([^\s()\[\]<>]*\))+)\)"
)


def _page_markdown(url: str) -> tuple[str, str | None]:
    """Your page as the provider's markdown, or ("", error). ~$0.003/call
    (on_page/content_parsing, no JS rendering)."""
    body = [{"url": url, "markdown_view": True}]
    res = _call("on_page/content_parsing/live", body, timeout=60)
    try:
        task = res["tasks"][0]
        if task.get("status_code") != 20000:
            return "", task.get("status_message")
        items = task["result"][0].get("items") or []
        if not items:
            return "", "page had no parseable content (crawler found nothing to read)"
        item = items[0]
        if item.get("status_code") and item["status_code"] >= 400:
            return "", f"page returned HTTP {item['status_code']}"
        return item.get("page_as_markdown") or "", None
    except Exception as e:
        return "", str(e)


def _page_read(url: str, markdown: str) -> dict:
    links = _MD_LINK_URL_RE.findall(markdown)
    domains = []
    for link in links:
        domain = urllib.parse.urlsplit(link).hostname
        if domain and domain not in domains:
            domains.append(domain)
    body = _page_body(markdown)
    return {
        "url": url,
        **_parse_opening(body),
        "outline": _outline(body),
        "has_table": _has_table(body),
        "num_links_out": len(links),
        "linked_domains": domains[:10],
    }


def page_structure(url: str) -> dict:
    """Same structural read as citation_structure, applied to your own
    page instead of the AI-cited answer, so the two are directly
    comparable. The opening is read from the page body, past the title and
    page chrome (see _page_body). Outbound links stand in for "sources cited"
    since a normal webpage has no DataForSEO-supplied source list."""
    markdown, err = _page_markdown(url)
    if err:
        return {"url": url, "error": err}
    try:
        return _page_read(url, markdown)
    except Exception as e:
        return {"url": url, "error": str(e)}


# Words that say nothing about WHAT a section covers ("Find out why...",
# "Build an early-warning system"), ignored when checking whether your page
# covers a point the cited answer makes.
_COVERAGE_STOPWORDS = _RESTATE_FILLER | {
    "find", "out", "get", "make", "build", "use", "using", "set", "right", "fast", "faster",
    "early", "don", "t", "can", "should", "will", "more", "most", "less", "best", "top",
    "way", "tip", "step", "before", "after", "when", "where", "which", "that", "this", "these",
    "not", "no", "from", "into", "than", "then", "they", "their", "our", "we", "my", "at",
    "by", "be", "or", "as", "if", "so", "up", "about", "who", "key", "good", "new", "practical",
    "approach", "simple", "quick", "common", "important", "thing",
}


def _possibly_missing(outline: list[str], page_words: set[str], keyword: str) -> list[str]:
    """Heads of the cited answer whose distinctive words mostly do not appear
    anywhere on your page. Word overlap, not meaning: a point covered in other
    words reads as missing, so this is a list to check, not a verdict."""
    skip = _norm_words(keyword) | _COVERAGE_STOPWORDS
    missing = []
    for head in outline:
        key = {w for w in _norm_words(head) - skip if len(w) > 2}
        if key and len(key & page_words) * 2 < len(key):
            missing.append(head)
    return missing


def _fix_brief(keyword: str, winning: dict, yours: dict, missing: list[str]) -> list[str]:
    """What to change on your page, most important first, as instructions the
    calling agent can carry out. Only checks that failed produce a line."""
    brief = []
    w_open, y_open = winning["opening_word_count"], yours["opening_word_count"]
    if w_open > 0 and y_open > w_open * 2:
        brief.append(
            f'Rewrite your opening as a direct answer to "{keyword}" in no more than '
            f"{max(w_open, 20)} words, before any background. The cited answer gets to the "
            f"point in {w_open} words; yours takes {y_open}."
        )
    if winning["opening_has_number"] and not yours["opening_has_number"]:
        brief.append("Put one concrete number in the opening (a figure, a timeframe or a count), as the cited answer does.")
    if winning["leads_with_list"] and not yours["leads_with_list"]:
        brief.append("Follow the opening straight away with a list, not paragraphs; the cited answer leads with one.")
    n_w, n_y = len(winning["outline"]), len(yours["outline"])
    if n_w >= 3 and n_y * 2 < n_w:
        brief.append(
            f"Break the page into about {n_w} sections, each under a heading that names one action "
            f"or answer; the cited answer has {n_w}, your page has {n_y}."
        )
    if missing:
        brief.append(
            "Add coverage for these points the cited answer makes, which your page does not appear "
            "to cover (check each, since this is word matching): " + "; ".join(missing) + "."
        )
    if winning["has_table"] and not yours["has_table"]:
        brief.append("Add a table; the cited answer uses one to lay options out side by side.")
    if winning["num_sources_cited"] > yours["num_links_out"]:
        brief.append(
            f"Cite at least {winning['num_sources_cited']} reputable external sources, linked next to "
            f"the claims they support; your page links out to {yours['num_links_out']}."
        )
    if brief:
        brief.append("Write every change in your own words from your own facts; do not copy the cited answer's wording.")
    else:
        brief.append("No structural change indicated: your page already matches the cited answer on "
                     "every check here, so the gap is most likely off-site, not on this page.")
    brief.append(_off_site_line(winning.get("source_mix") or source_mix(winning.get("source_domains") or [])))
    return brief


def _off_site_line(mix: dict) -> str:
    """The off-site step, always last in the brief. Page shape gets a page into
    the running; whether it is cited is decided mostly by what other sites say
    about the brand, which is the work a page rewrite cannot do."""
    if mix["community_domains"]:
        return (
            f"Beyond this page: {mix['community_pct']:g}% of the cited answer's sources are community "
            f"sites ({', '.join(mix['community_domains'])}). Get your brand genuinely discussed there: "
            "a useful answer in a relevant thread, a video walkthrough, a post people can react to. "
            "Never fake accounts or bought comments."
        )
    others = ", ".join(mix["other_domains"][:3])
    return (
        "Beyond this page: the cited answer relies on other websites"
        + (f" ({others})" if others else "")
        + ", not community sites, so a mention or listing on sites like these matters more than a rewrite. "
        "find_citation_leaders shows who else wins this topic."
    )


def citation_gap(keyword: str, your_url: str,
                 country: str = DEFAULT_COUNTRY, language: str = DEFAULT_LANGUAGE,
                 engine: str = "chat_gpt") -> dict:
    """Diff your own page's structure against the winning AI-cited
    answer's structure for the same keyword, as concrete gaps to close
    rather than two separate reports read side by side."""
    winning = citation_structure(keyword, country=country, language=language, engine=engine)
    if winning.get("error"):
        return {"keyword": keyword, "your_url": your_url, "error": f"couldn't analyze the winning answer: {winning['error']}"}
    markdown, err = _page_markdown(your_url)
    if err:
        return {"keyword": keyword, "your_url": your_url, "error": f"couldn't fetch/parse your_url: {err}"}
    try:
        yours = _page_read(your_url, markdown)
    except Exception as e:
        return {"keyword": keyword, "your_url": your_url, "error": f"couldn't fetch/parse your_url: {e}"}
    page_words = _norm_words(strip_markdown(_page_body(markdown)))
    missing = _possibly_missing(winning["outline"], page_words, keyword)

    gaps = []
    if winning["leads_with_list"] and not yours["leads_with_list"]:
        gaps.append("Winning answer leads with a list; your page opens with a paragraph.")
    if winning["opening_word_count"] > 0 and yours["opening_word_count"] > winning["opening_word_count"] * 2:
        gaps.append(
            f"Winning opening is {winning['opening_word_count']} words before the point; "
            f"yours is {yours['opening_word_count']}."
        )
    if winning["opening_has_number"] and not yours["opening_has_number"]:
        gaps.append("Winning opening states a number/stat up front; yours doesn't.")
    if winning["num_sources_cited"] > yours["num_links_out"]:
        gaps.append(
            f"Winning answer cites {winning['num_sources_cited']} sources; "
            f"your page links out to {yours['num_links_out']}."
        )
    return {
        "keyword": keyword, "your_url": your_url, "winning": winning, "yours": yours, "gaps": gaps,
        "possibly_missing": missing,
        "fix_brief": _fix_brief(keyword, winning, yours, missing),
    }
