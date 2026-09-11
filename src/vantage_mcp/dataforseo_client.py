"""Shared DataForSEO client for AI-answer-engine visibility checks.

Domain/keyword/platform are call-time parameters rather than baked-in
constants, since this serves arbitrary callers, not one fixed site.

Auth: DATAFORSEO_USERNAME / DATAFORSEO_PASSWORD from env.
"""

import base64
import json
import os
import re
import urllib.error
import urllib.request

API = "https://api.dataforseo.com/v3"


class DataForSEOError(RuntimeError):
    pass


def _auth_header() -> str:
    user = os.environ.get("DATAFORSEO_USERNAME")
    password = os.environ.get("DATAFORSEO_PASSWORD")
    if not user or not password:
        raise DataForSEOError("DATAFORSEO_USERNAME / DATAFORSEO_PASSWORD not set in environment")
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def _call(path: str, payload: list, timeout: int = 130) -> dict:
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


def domain_mentions(domain: str, platform: str = "chat_gpt") -> int | None:
    """How many times DataForSEO's tracked corpus cites this domain,
    on this AI platform. ~$0.10/call."""
    body = [{"target": [{"domain": domain}], "platform": platform}]
    res = _call("ai_optimization/llm_mentions/target_metrics/live", body)
    try:
        agg = res["tasks"][0]["result"][0]["aggregated_metrics"]
        group = _first_group_list(agg)
        return sum(g.get("mentions", 0) for g in group)
    except Exception:
        return None


def citation_leaders(keyword: str, platform: str = "chat_gpt", limit: int = 5) -> dict:
    """Who dominates AI-answer citations for this keyword/topic, and
    whether the given domain shows up in that list. ~$0.15/call.

    `limit` is the provider's own cap on how many domains come back (max 10),
    echoed in the result as `top_domains_limit` so a caller can tell
    "not in the top N" apart from "not cited anywhere" - those are different
    claims and this endpoint can only ever support the first one."""
    body = [{"target": [{"keyword": keyword}], "items_list_limit": limit, "platform": platform}]
    res = _call("ai_optimization/llm_mentions/top_domains/live", body)
    try:
        items = res["tasks"][0]["result"][0]["items"]
        leaders = []
        for it in items:
            group = _first_group_list(it)
            leaders.append({"domain": it["key"], "mentions": group[0].get("mentions") if group else None})
        return {"keyword": keyword, "platform": platform, "top_domains": leaders,
                "top_domains_limit": limit}
    except Exception as e:
        # No "top_domains": [] here on purpose. An empty list next to an error
        # was previously indistinguishable from a real, confirmed-empty result -
        # server.py's compare_domain_present was computed over this same empty
        # list either way, which is how a parse failure turned into a false
        # "not cited" answer. An error response now carries no top_domains key
        # at all, so the caller cannot accidentally read it as a real leaderboard.
        return {"keyword": keyword, "platform": platform, "error": str(e)}


def citation_trend(domain: str, platform: str = "chat_gpt") -> dict:
    """Month-by-month mention counts for a domain since DataForSEO's
    history began (2025-08-01), oldest to newest. Priced at $0/call on
    every real call made verifying this - unlike domain_mentions/
    citation_leaders above, which run ~$0.10-0.15/call. A month with no
    tracked mentions comes back with no "metrics" key at all rather than
    zeros - real behavior found calling this live, not assumed from the
    docs - so that gets normalized to an explicit 0 here rather than
    silently dropped."""
    body = [{"target": [{"domain": domain}], "platform": platform}]
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


def strip_markdown(text: str) -> str:
    out = text or ""
    for pattern, repl in _MD_MARKERS:
        out = pattern.sub(repl, out)
    return " ".join(out.split())


def _parse_opening(markdown: str) -> dict:
    """Shared structural read of a markdown document's opening, used
    both for the AI-cited winning answer (citation_structure) and for
    a caller's own page (page_structure), so the two are computed the
    exact same way and stay directly comparable."""
    list_match = _LIST_RE.search(markdown)
    cutoff = list_match.start() if list_match else len(markdown)
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


# A citation chip in the answer's markdown is a link whose visible text is only
# a domain, e.g. "([nhs.uk](https://www.nhs.uk/...))". That is the answer citing
# a source, not naming one, so chips are removed before looking for a mention.
_CITATION_CHIP_RE = re.compile(r"\[\s*[\w.-]+\.[a-z]{2,}\s*\]\([^)]*\)", re.IGNORECASE)


def mentions_any(markdown: str, terms: list[str]) -> bool:
    """True if the answer's own text names any of `terms` (whole word, case
    insensitive), ignoring domain-only citation links. "Named" is a different
    claim from "cited": an answer can recommend a product without linking it,
    and can link a page it never names. Terms under 3 characters are skipped,
    since they match inside ordinary words."""
    text = strip_markdown(_CITATION_CHIP_RE.sub("", markdown or ""))
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


def citation_structure(keyword: str, mention_terms: list[str] | None = None) -> dict:
    """Structural shape of the AI-generated answer actually cited for
    this keyword: does it lead with a list, how long is the opening,
    how many sources does it cite, which domains. ~$0.004/call.
    With `mention_terms`, also reports whether the answer text names any of
    them ("mentioned"), from the same response at no extra cost."""
    body = [{"keyword": keyword, "language_code": "en", "location_name": "United States", "force_web_search": True}]
    res = _call("ai_optimization/chat_gpt/llm_scraper/live/advanced", body, timeout=130)
    try:
        task = res["tasks"][0]
        if task.get("status_code") != 20000:
            return {"keyword": keyword, "error": task.get("status_message")}
        result = task["result"][0]
        markdown = result.get("markdown") or ""
        sources = result.get("sources") or []
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
        # The provider can list the same domain twice (e.g. two different
        # pages on bitwarden.com cited separately), which inflated both the
        # visible list and num_sources_cited - a "9 sources" answer with 2
        # duplicates is really 7 distinct sites. Deduped by domain, order
        # preserved (first mention wins), before counting or truncating,
        # so num_sources_cited and the list it describes always agree.
        domains = []
        for src in sources:
            d = src.get("domain")
            if d and d not in domains:
                domains.append(d)
        return {
            "keyword": keyword,
            **parsed,
            "detail_preview": detail_preview,
            "num_sources_cited": len(domains[:10]),
            "source_domains": domains[:10],
            **({"mentioned": mentions_any(markdown, mention_terms)} if mention_terms else {}),
        }
    except Exception as e:
        return {"keyword": keyword, "error": str(e)}


# A web page's markdown does not start where its content does. DataForSEO's
# page_as_markdown puts the H1 first, then whatever the page shows before the
# body: related-article cards, "Getting started" style jump labels, bylines,
# "5 min read". Taking the first block of that as the opening measured the
# title plus a link to another article, and reported "yours is 19 words" for
# an opening nobody wrote (found 2026-09-11 by an internal test). Page chrome
# is skipped line by line until the first line of real content: a heading, a
# line that is only links or images, and a short label (under
# _CHROME_MAX_WORDS words, not ending like a sentence) are all chrome. The AI
# answer is NOT put through this: its first line is the answer, headings
# included, so that side is measured exactly as before.
_LINK_OR_IMAGE_RE = re.compile(r"!?\[[^\]]*\]\([^)]*\)")
_SENTENCE_END_RE = re.compile(r"[.!?:]$")
_CHROME_MAX_WORDS = 5


def _page_body(markdown: str) -> str:
    """The page's markdown from its first line of real content onward, with a
    blank line forced before every heading so a paragraph that runs straight
    into the next heading still ends there. Falls back to the whole markdown
    if every line looks like chrome, rather than measuring nothing."""
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


def page_structure(url: str) -> dict:
    """Same structural read as citation_structure, applied to your own
    page instead of the AI-cited answer, so the two are directly
    comparable. The opening is read from the page body, past the title and
    page chrome (see _page_body). Outbound links stand in for "sources cited"
    since a normal webpage has no DataForSEO-supplied source list.
    ~$0.003/call (on_page/content_parsing, no JS rendering)."""
    body = [{"url": url, "markdown_view": True}]
    res = _call("on_page/content_parsing/live", body, timeout=60)
    try:
        task = res["tasks"][0]
        if task.get("status_code") != 20000:
            return {"url": url, "error": task.get("status_message")}
        items = task["result"][0].get("items") or []
        if not items:
            return {"url": url, "error": "page had no parseable content (crawler found nothing to read)"}
        item = items[0]
        if item.get("status_code") and item["status_code"] >= 400:
            return {"url": url, "error": f"page returned HTTP {item['status_code']}"}
        markdown = item.get("page_as_markdown") or ""
        links = re.findall(r"\[[^\]]*\]\((https?://[^)\s]+)\)", markdown)
        domains = []
        for link in links:
            domain = link.split("/")[2] if link.count("/") >= 2 else link
            if domain not in domains:
                domains.append(domain)
        return {
            "url": url,
            **_parse_opening(_page_body(markdown)),
            "num_links_out": len(links),
            "linked_domains": domains[:10],
        }
    except Exception as e:
        return {"url": url, "error": str(e)}


def citation_gap(keyword: str, your_url: str) -> dict:
    """Diff your own page's structure against the winning AI-cited
    answer's structure for the same keyword, as concrete gaps to close
    rather than two separate reports read side by side."""
    winning = citation_structure(keyword)
    if winning.get("error"):
        return {"keyword": keyword, "your_url": your_url, "error": f"couldn't analyze the winning answer: {winning['error']}"}
    yours = page_structure(your_url)
    if yours.get("error"):
        return {"keyword": keyword, "your_url": your_url, "error": f"couldn't fetch/parse your_url: {yours['error']}"}

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
    return {"keyword": keyword, "your_url": your_url, "winning": winning, "yours": yours, "gaps": gaps}
