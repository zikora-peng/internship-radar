"""
Send a daily Telegram summary of new internship matches, with a link to
the Google Doc holding the full log.
Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to be set as env vars
(see README.md for how to create a bot and find your chat id).
"""
import os
import urllib.parse
import urllib.request

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LISTED = 10  # cap the message length


def send_summary(new_count, strong_matches, threshold, doc_link=None,
                 pending_count=0):
    """pending_count: listings that could not be scored this run. They were not
    written to the doc and stay unseen, so they retry next run. Surfaced here
    because a scorer that quietly fails every day would otherwise look
    identical to a quiet job market."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram credentials not set — skipping notification.")
        return

    pending_line = (
        f"\n⏳ {pending_count} listing(s) pending re-score — scoring failed, "
        f"will retry next run." if pending_count else ""
    )

    if new_count == 0:
        text = "🔍 Internship Radar: no new postings today." + pending_line
    else:
        lines = [
            f"🔍 Internship Radar: {new_count} new posting(s) found, "
            f"{len(strong_matches)} scored {threshold}+.\n"
        ]
        for item in strong_matches[:MAX_LISTED]:
            lines.append(
                f"⭐ {item['score']}/100 — {item.get('company_name', '')} · "
                f"{item.get('title', '')}\n{item.get('pitch', '')}\n{item.get('url', '')}\n"
            )
        if len(strong_matches) > MAX_LISTED:
            lines.append(f"...and {len(strong_matches) - MAX_LISTED} more in the doc.")
        elif not strong_matches:
            lines.append("None cleared the alert threshold today — full list is in the doc.")
        if pending_line:
            lines.append(pending_line.lstrip("\n"))
        if doc_link:
            lines.append(f"\n📄 Full log: {doc_link}")
        text = "\n".join(lines)

    url = TELEGRAM_API.format(token=token)
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(url, data=data)
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:  # noqa: BLE001 - don't fail the whole run over a notify error
        print(f"Warning: Telegram send failed: {e}")
