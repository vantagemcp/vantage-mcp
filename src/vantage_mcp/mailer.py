"""Email an account's API key to the address on file (Resend).

Used by the OAuth sign-in page (server.py): a key is shown once, a person
who does not save it, or whose client misses the hand-back, is otherwise
locked out of an account they just created, because an email alone never
signs in to an existing account (auth.py). A copy in the inbox is the way
back in. billing.py has its own sender for the web signup page and is left
alone on purpose.

Best-effort by design: a failed send must never fail or slow a sign-in, so
nothing here raises and callers do not wait on it.
"""

import html
import os

import httpx2

RESEND_API_KEY = os.environ.get("VANTAGE_RESEND_API_KEY", "")
FROM = "Vantage <noreply@vantagemcp.dev>"
BASE_URL = "https://vantagemcp.dev"

_INTRO = {
    "signup": "Your free Vantage account is ready. This is its API key, the same one shown on the page "
              "you just saw, kept here in case you did not save it.",
    "resend": "You asked for a new API key for this address. Here it is. Your existing keys keep working, "
              "and nothing was revoked.",
}


def _body(api_key: str, reason: str) -> tuple[str, str]:
    intro = _INTRO.get(reason, _INTRO["signup"])
    steps = ("To connect an app: open its sign-in, choose \"Already have a Vantage API key?\" and paste "
             "this key. If you did not ask for this, you can ignore the email.")
    text = f"{intro}\n\n{api_key}\n\n{steps}\n\nVantage, {BASE_URL}\n"
    # Light by default, dark through prefers-color-scheme: every colour is a class so a client that
    # honours the media query switches cleanly, and one that ignores it still shows readable light.
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark"><meta name="supported-color-schemes" content="light dark">
<style>
:root {{ color-scheme: light dark; supported-color-schemes: light dark; }}
@media (prefers-color-scheme: dark) {{
  .bg {{ background:#010403 !important; }}
  .tx {{ color:#c9d4d1 !important; }}
  .hd {{ color:#f8fdfc !important; }}
  .key {{ background:#0b1512 !important; border-color:#1d2b27 !important; color:#ecf4f2 !important; }}
  .mut {{ color:#7d8a86 !important; }}
  .lk {{ color:#35d3b0 !important; }}
}}
</style></head><body class="bg" style="margin:0; padding:32px 24px; background:#ffffff;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
<table role="presentation" width="520" cellpadding="0" cellspacing="0" style="max-width:520px;">
<tr><td class="tx" style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; font-size:15px; line-height:1.6; color:#33403c;">
<p class="hd" style="margin:0 0 18px; color:#0b1512; font-size:20px; font-weight:600;">Your Vantage API key</p>
<p style="margin:0 0 18px;">{html.escape(intro)}</p>
<p class="key" style="margin:0 0 18px; padding:14px; background:#f1f5f4; border:1px solid #d3ddda; border-radius:8px;
 font-family:'SF Mono',Menlo,Consolas,monospace; font-size:14px; color:#0b1512; word-break:break-all;">
<code>{html.escape(api_key)}</code></p>
<p style="margin:0 0 18px;">{html.escape(steps)}</p>
<p class="mut" style="margin:0; color:#5d6b67;">Vantage, <a class="lk" href="{BASE_URL}" style="color:#0f7a62;">vantagemcp.dev</a></p>
</td></tr></table></td></tr></table></body></html>"""
    return text, page


async def send_key_email(email: str, api_key: str, reason: str = "signup") -> bool:
    """True if Resend accepted the message. Never raises."""
    if not RESEND_API_KEY or not email or not api_key:
        return False
    try:
        text, page = _body(api_key, reason)
        async with httpx2.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                json={"from": FROM, "to": email, "subject": "Your Vantage API key", "html": page, "text": text},
            )
        return 200 <= r.status_code < 300
    except Exception:
        return False
