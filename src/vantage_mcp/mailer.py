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
    page = f"""<!doctype html><html lang="en"><body style="margin:0; padding:32px 24px; background:#010403;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
<table role="presentation" width="520" cellpadding="0" cellspacing="0" style="max-width:520px;">
<tr><td style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; font-size:15px; line-height:1.6; color:#c9d4d1;">
<p style="margin:0 0 18px; color:#f8fdfc; font-size:20px; font-weight:600;">Your Vantage API key</p>
<p style="margin:0 0 18px;">{html.escape(intro)}</p>
<p style="margin:0 0 18px; padding:14px; background:#0b1512; border:1px solid #1d2b27; border-radius:8px;
 font-family:'SF Mono',Menlo,Consolas,monospace; font-size:14px; color:#ecf4f2; word-break:break-all;">
<code>{html.escape(api_key)}</code></p>
<p style="margin:0 0 18px;">{html.escape(steps)}</p>
<p style="margin:0; color:#7d8a86;">Vantage, <a href="{BASE_URL}" style="color:#35d3b0;">vantagemcp.dev</a></p>
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
