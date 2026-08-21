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
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    return float(d["tasks"][0]["result"][0]["money"]["balance"])


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
    whether the given domain shows up in that list. ~$0.15/call."""
    body = [{"target": [{"keyword": keyword}], "items_list_limit": limit, "platform": platform}]
    res = _call("ai_optimization/llm_mentions/top_domains/live", body)
    try:
        items = res["tasks"][0]["result"][0]["items"]
        leaders = []
        for it in items:
            group = _first_group_list(it)
            leaders.append({"domain": it["key"], "mentions": group[0].get("mentions") if group else None})
        return {"keyword": keyword, "platform": platform, "top_domains": leaders}
    except Exception as e:
        return {"keyword": keyword, "platform": platform, "top_domains": [], "error": str(e)}


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
    opening = markdown[:cutoff].strip()
    return {
        "leads_with_list": bool(_LIST_RE.match(markdown.lstrip())),
        "opening_word_count": len(opening.split()),
        "opening_has_number": bool(re.search(r"\d", opening)),
    }


def citation_structure(keyword: str) -> dict:
    """Structural shape of the AI-generated answer actually cited for
    this keyword: does it lead with a list, how long is the opening,
    how many sources does it cite, which domains. ~$0.004/call."""
    body = [{"keyword": keyword, "language_code": "en", "location_name": "United States", "force_web_search": True}]
    res = _call("ai_optimization/chat_gpt/llm_scraper/live/advanced", body, timeout=130)
    try:
        task = res["tasks"][0]
        if task.get("status_code") != 20000:
            return {"keyword": keyword, "error": task.get("status_message")}
        result = task["result"][0]
        markdown = result.get("markdown") or ""
        sources = result.get("sources") or []
        return {
            "keyword": keyword,
            **_parse_opening(markdown),
            "num_sources_cited": len(sources),
            "source_domains": [s.get("domain") for s in sources][:10],
        }
    except Exception as e:
        return {"keyword": keyword, "error": str(e)}


def page_structure(url: str) -> dict:
    """Same structural read as citation_structure, applied to your own
    page instead of the AI-cited answer, so the two are directly
    comparable. Outbound links stand in for "sources cited" since a
    normal webpage has no DataForSEO-supplied source list.
    ~$0.003/call (on_page/content_parsing, no JS rendering)."""
    body = [{"url": url, "markdown_view": True}]
    res = _call("on_page/content_parsing/live", body, timeout=60)
    try:
        task = res["tasks"][0]
        if task.get("status_code") != 20000:
            return {"url": url, "error": task.get("status_message")}
        item = task["result"][0]["items"][0]
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
            **_parse_opening(markdown),
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
