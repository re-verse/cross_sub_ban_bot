"""
DM inbox listener for help requests.

Modmail handles the four mutating commands (status, pardon, exempt) and
help, but a non-trivial fraction of mods will reflexively PM the bot
account directly instead of going through their own sub's modmail. This
module catches that path and auto-replies with the help text, gated on
the sender being a moderator of at least one trusted sub.

Strictly read-only:
- Only /xsub help (and unknown /xsub commands) get a reply
- Mutating commands over DM are deliberately not supported. Asking a
  user to do those through modmail keeps every action attributable to
  a specific sub's mod team, which matters for audit + abuse cases.
- Non-mods get no reply at all (no engagement signal to drive-bys).
"""
import os
import prawcore
from bot_config import reddit, TRUSTED_SUBS
from core_utils import is_mod

# Cap inbox scan per run. Reddit's unread inbox shouldn't be large for a
# bot account, but defensively bound it so a flood can't stall a cron tick.
_MAX_INBOX_ITEMS = 50

_HELP_TEXT = (
    "**Cross-Sub Ban Pact Bot — Help**\n\n"
    "I can take commands via either:\n"
    "1. **Modmail to your sub** — preferred for any action that changes "
    "state (pardon, exempt). Mention me or just send the command.\n"
    "2. **Direct message to me** — only `/xsub help` works over DM. "
    "All mutating commands must go through your sub's modmail so the "
    "action is attributable to a specific mod team.\n\n"
    "    /xsub help                       — this message\n"
    "    /xsub status u/username          — show this user's status across the network\n"
    "    /xsub history u/username         — chronological audit trail (modmail only)\n"
    "    /xsub pardon u/username          — forgive + unban a user (origin-sub mods only, modmail only)\n"
    "    /xsub exempt u/username          — exempt user from bans in your sub only (modmail only)\n"
    "    /xsub super ban u/username ...   — manual cross-sub ban (bot owner only, modmail only)\n\n"
    "The pact triggers on bans whose reason is exactly "
    "**Auto XSub Pact Ban**.\n\n"
    "Public log: https://re-verse.github.io/cross_sub_ban_bot/public_ban_log.html"
)


def _bot_username():
    """Return the bot's own Reddit username (lowercased)."""
    name = os.environ.get("REDDIT_USERNAME", "")
    if name:
        return name.lower()
    try:
        return reddit.user.me().name.lower()
    except Exception:
        return ""


def _is_trusted_mod(sender):
    """
    True if `sender` is a mod of at least one trusted sub.

    Iterates trusted subs in order and returns on first match. For a mod
    of an early-listed sub this is cheap. For a non-mod it walks the
    full list, which is the worst case (~9 API calls). That's fine at
    20-min cadence with the inbox cap above.
    """
    for sub in TRUSTED_SUBS:
        try:
            sr = reddit.subreddit(sub)
            if is_mod(sr, sender):
                return True
        except Exception:
            continue
    return False


def check_dm_inbox(dry_run=False):
    """
    Read unread DMs to the bot and respond to /xsub help from trusted mods.

    dry_run: if True, log intent but don't reply or mark anything read.
    """
    print("[DM-INBOX] Checking for /xsub help DMs...")
    bot_name = _bot_username()
    handled = 0
    skipped = 0

    try:
        items = list(reddit.inbox.unread(limit=_MAX_INBOX_ITEMS))
    except Exception as e:
        print(f"[DM-INBOX-ERROR] Could not read inbox: {e}")
        return

    for item in items:
        # Skip modmail items (they have a different shape and are handled
        # by modmail_utils). Reddit puts modmail in the same inbox stream
        # but flags it via the `was_comment` / subject pattern.
        try:
            kind = getattr(item, "kind", "") or item.__class__.__name__.lower()
            if "comment" in kind:
                # Comment replies / username mentions — not our path.
                continue
        except Exception:
            pass

        author_obj = getattr(item, "author", None)
        if author_obj is None:
            continue
        sender = (getattr(author_obj, "name", "") or "").lower()
        if not sender or sender == bot_name:
            continue

        body = (getattr(item, "body", "") or "").strip()
        if not body:
            continue

        body_l = body.lower().lstrip("> ").strip()
        if not body_l.startswith("/xsub"):
            # Not a command DM — don't even confirm we read it. The bot
            # account isn't a help desk for arbitrary user questions.
            continue

        # Gate on mod status before doing anything else
        if not _is_trusted_mod(sender):
            print(f"[DM-INBOX-SKIP] u/{sender} sent /xsub but is not a trusted-sub mod")
            skipped += 1
            if not dry_run:
                try:
                    item.mark_read()
                except Exception:
                    pass
            continue

        parts = body_l.split()
        cmd = parts[1] if len(parts) >= 2 else ""

        if cmd == "help":
            reply_body = _HELP_TEXT
            log_tag = "help"
        elif cmd in ("status", "history", "pardon", "exempt", "super"):
            reply_body = (
                f"ℹ️ `/xsub {cmd}` is only available via modmail to your own sub, "
                "not via DM. This keeps every action attributable to a specific "
                "mod team. Send the command as modmail from your sub instead.\n\n"
                "Send `/xsub help` here any time for the full command list."
            )
            log_tag = f"{cmd}-redirect"
        else:
            reply_body = (
                f"⚠️ Unknown command `/xsub {cmd or '(empty)'}`. "
                "Send `/xsub help` for the command list."
            )
            log_tag = f"unknown:{cmd or 'empty'}"

        if dry_run:
            print(f"[DRY-RUN][DM-INBOX-REPLY] u/{sender} tag={log_tag}: would reply")
            continue

        try:
            item.reply(reply_body)
            item.mark_read()
            print(f"[DM-INBOX-REPLY] u/{sender} tag={log_tag}: replied")
            handled += 1
        except Exception as e:
            print(f"[DM-INBOX-ERROR] reply to u/{sender} failed: {e}")

    if handled or skipped:
        print(f"[DM-INBOX] done: {handled} replied, {skipped} non-mod skipped")
