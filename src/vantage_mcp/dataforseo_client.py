"""Shared DataForSEO client for AI-answer-engine visibility checks.

Domain/keyword/platform are call-time parameters rather than baked-in
constants, since this serves arbitrary callers, not one fixed site.

Auth: DATAFORSEO_USERNAME / DATAFORSEO_PASSWORD from env.
"""

import base64
import json
import os
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

        import re

        list_re = re.compile(r"^\s*(?:[-*]|\d+\.)\s+", re.MULTILINE)
        list_match = list_re.search(markdown)
        cutoff = list_match.start() if list_match else len(markdown)
        para_end = markdown.find("\n\n")
        if para_end != -1 and para_end < cutoff:
            cutoff = para_end
        opening = markdown[:cutoff].strip()

        return {
            "keyword": keyword,
            "leads_with_list": bool(list_re.match(markdown.lstrip())),
            "opening_word_count": len(opening.split()),
            "opening_has_number": bool(re.search(r"\d", opening)),
            "num_sources_cited": len(sources),
            "source_domains": [s.get("domain") for s in sources][:10],
        }
    except Exception as e:
        return {"keyword": keyword, "error": str(e)}
