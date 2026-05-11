"""
Modmail command handler for the Cross-Sub Ban Pact bot.

Listens for these commands as the latest mod-authored message in a
modmail thread (state="new") on each trusted sub:

  /xsub help                  -> static reference card
  /xsub status u/<username>   -> facts from the bans DB (any trusted-sub mod)
  /xsub pardon u/<username>   -> forgive + propagate unban (origin-sub mods only)
  /xsub exempt u/<username>   -> exempt user from bans in this sub only

Behaviour notes:
- The bot will not reply twice to the same conversation (it checks for
  any prior message authored by itself). A new mod message after a bot
  reply will be treated as a fresh request.
- /xsub pardon now also propagates the unban across the network. The
  pre-Sheets-migration version only flipped the override flag, which
  left users still banned on every other sub. Mods reasonably expect
  pardon to mean "undo + prevent re-ban".
- Modmail iteration failures are caught per-sub and recorded against
  the health tracker so they surface alongside modlog access drift.
"""
from datetime import datetime
from bot_config import database, reddit, TRUSTED_SUBS
from core_utils import is_mod
from log_utils import log_public_action
import os
import prawcore

# Cap how many recent convos we look at per sub per run, so a long modmail
# history doesn't make every cron tick expensive.
_MAX_CONVOS_PER_SUB = 25

_HELP_TEXT = (
    "**Cross-Sub Ban Pact Bot — Help**\n\n"
    "I take commands in two ways:\n"
    "1. **Modmail to your sub** (this thread) — supports the full command set "
    "below. Send a command as the modmail body, I'll reply within 20 minutes.\n"
    "2. **DM to me directly** — only `/xsub help` works that way. "
    "Mutating commands have to go through modmail so the action is "
    "attributable to a specific mod team.\n\n"
    "    /xsub help                 — this message\n"
    "    /xsub status u/username    — show this user's status across the network\n"
    "    /xsub history u/username   — chronological audit trail for this user\n"
    "    /xsub pardon u/username    — forgive + unban a user (origin-sub mods only)\n"
    "    /xsub exempt u/username    — exempt this user from bans in your sub only\n\n"
    "The pact triggers on bans whose reason is exactly "
    "**Auto XSub Pact Ban**.\n\n"
    "Public log: https://re-verse.github.io/cross_sub_ban_bot/public_ban_log.html"
)


def _bot_username():
    """Return the bot's own Reddit username (lowercased) for self-detection."""
    name = os.environ.get("REDDIT_USERNAME", "")
    if name:
        return name.lower()
    try:
        return reddit.user.me().name.lower()
    except Exception:
        return ""


def _bot_already_replied(convo, bot_name):
    """Skip convos where we've already responded to avoid reply loops."""
    if not bot_name:
        return False
    for m in convo.messages:
        author = getattr(m.author, "name", "") or ""
        if author.lower() == bot_name:
            return True
    return False


def _parse_username(token):
    """Strip a leading u/ or /u/ off a username token, return lowercased."""
    t = token.strip().lstrip("/")
    if t.lower().startswith("u/"):
        t = t[2:]
    return t.strip()


def check_modmail(health_state=None, dry_run=False, propagate_unban=None):
    """
    Walk modmail on each trusted sub and act on /xsub commands.

    health_state: optional dict from health_utils.load_health() — if passed,
        modmail-access failures will be recorded against the per-sub tracker
        so persistent issues surface in [HEALTH-ALERT-PERSISTENT] lines.
    dry_run: if True, log intent but do not reply, mutate the DB, or
        propagate any unbans. Mirrors the DRY_RUN gate in the main bot.
    propagate_unban: optional callable(username, source_sub) used by
        /xsub pardon to apply the unban across the trusted-sub network.
        Passed in by the main bot so the propagation path is shared with
        the modlog-driven unban handler.
    """
    print("[MODMAIL] Checking for /xsub commands...")
    bot_name = _bot_username()

    for sub in TRUSTED_SUBS:
        try:
            sr = reddit.subreddit(sub)
            seen = 0
            for convo in sr.modmail.conversations(state="new"):
                seen += 1
                if seen > _MAX_CONVOS_PER_SUB:
                    break
                _handle_convo(
                    convo, sr, sub, bot_name,
                    dry_run=dry_run,
                    propagate_unban=propagate_unban,
                )
        except prawcore.exceptions.Forbidden as e:
            print(f"[MODMAIL-WARN] Forbidden on r/{sub}: {e}")
            if health_state is not None:
                from health_utils import record_failure
                record_failure(health_state, f"{sub}:modmail", e)
        except prawcore.exceptions.NotFound as e:
            print(f"[MODMAIL-WARN] NotFound on r/{sub}: {e}")
            if health_state is not None:
                from health_utils import record_failure
                record_failure(health_state, f"{sub}:modmail", e)
        except Exception as e:
            print(f"[MODMAIL-ERROR] r/{sub}: {e}")


def _handle_convo(convo, sr, sub, bot_name, dry_run=False, propagate_unban=None):
    """Process a single conversation if it looks like a fresh /xsub command."""
    if not convo.messages:
        return
    if _bot_already_replied(convo, bot_name):
        return

    last = convo.messages[-1]
    body = (getattr(last, "body_markdown", "") or "").strip()
    sender = (getattr(last.author, "name", "") or "").lower()
    if not sender or not body:
        return
    if sender == bot_name:
        return  # last message was ours; nothing to act on
    if not is_mod(sr, sender):
        return

    body_l = body.lower()
    # Be tolerant of leading whitespace / quoting characters
    body_l = body_l.lstrip("> ").strip()
    if not body_l.startswith("/xsub"):
        return

    parts = body_l.split()
    if len(parts) < 2:
        return
    cmd = parts[1]

    if cmd == "help":
        _reply(convo, _HELP_TEXT, dry_run=dry_run, sub=sub, sender=sender, cmd="help")
        return

    if len(parts) < 3:
        _reply(
            convo,
            f"⚠️ `/xsub {cmd}` needs a username. Try `/xsub help` for the full list.",
            dry_run=dry_run, sub=sub, sender=sender, cmd=cmd,
        )
        return

    user = _parse_username(parts[2])
    if not user:
        _reply(
            convo,
            "⚠️ Could not parse the username. Format: `/xsub <cmd> u/username`",
            dry_run=dry_run, sub=sub, sender=sender, cmd=cmd,
        )
        return

    if cmd == "status":
        _handle_status(convo, user, dry_run=dry_run, sub=sub, sender=sender)
    elif cmd == "history":
        _handle_history(convo, user, dry_run=dry_run, sub=sub, sender=sender)
    elif cmd == "pardon":
        _handle_pardon(
            convo, user, sub, sender,
            dry_run=dry_run, propagate_unban=propagate_unban,
        )
    elif cmd == "exempt":
        _handle_exempt(convo, user, sub, sender, dry_run=dry_run)
    else:
        _reply(
            convo,
            f"⚠️ Unknown command `/xsub {cmd}`. Try `/xsub help`.",
            dry_run=dry_run, sub=sub, sender=sender, cmd=cmd,
        )


def _handle_status(convo, user, dry_run, sub, sender):
    rows = database.find_user_records(user)
    if not rows:
        _reply(
            convo,
            f"📭 No record of u/{user} in the pact database.",
            dry_run=dry_run, sub=sub, sender=sender, cmd="status",
        )
        return

    # Pick the most informative row to summarise (any forgiven row wins;
    # otherwise the most recent insert).
    forgiven_row = next(
        (r for r in rows if (r.get("manual_override") or "").lower() == "yes"),
        None,
    )
    primary = forgiven_row or rows[0]
    src = primary.get("source_sub") or "?"
    when = primary.get("timestamp") or "?"
    forgiven = (primary.get("manual_override") or "").lower() == "yes"
    forgive_by = primary.get("moderator_name") or ""
    forgive_sub = primary.get("mod_sub") or ""
    forgive_when = primary.get("forgive_timestamp") or ""
    exempt = (primary.get("exempt_subs") or "").strip()

    lines = [f"**Status for u/{user}**", ""]
    lines.append(f"- Origin sub: {src}")
    lines.append(f"- First recorded: {when}")
    if forgiven:
        details = []
        if forgive_by:
            details.append(f"by u/{forgive_by}")
        if forgive_sub:
            details.append(f"in {forgive_sub}")
        if forgive_when:
            details.append(f"on {forgive_when}")
        suffix = f" ({', '.join(details)})" if details else ""
        lines.append(f"- Forgiven: ✅ yes{suffix}")
    else:
        lines.append("- Forgiven: ❌ no — currently banned across the pact network")
    if exempt:
        lines.append(f"- Exempt in: {exempt}")
    if len(rows) > 1:
        lines.append(f"- Records in DB: {len(rows)}")

    _reply(
        convo, "\n".join(lines),
        dry_run=dry_run, sub=sub, sender=sender, cmd="status",
    )


def _handle_history(convo, user, dry_run, sub, sender):
    """Render a chronological audit trail across all DB rows for this user."""
    rows = database.find_user_records(user)
    if not rows:
        _reply(
            convo,
            f"📭 No record of u/{user} in the pact database.",
            dry_run=dry_run, sub=sub, sender=sender, cmd="history",
        )
        return

    # find_user_records returns most-recent-first; reverse for chronological.
    events = []
    exempt_lines = []
    for r in reversed(rows):
        ts = r.get("timestamp") or "?"
        src = r.get("source_sub") or "?"
        actor = r.get("moderator_name") or "?"
        events.append(
            f"- `{ts}`  **BANNED** in {src} by u/{actor}"
        )
        if (r.get("manual_override") or "").lower() == "yes":
            f_ts = r.get("forgive_timestamp") or "?"
            f_by = r.get("moderator_name") or "?"
            f_sub = r.get("mod_sub") or "?"
            events.append(
                f"- `{f_ts}`  **FORGIVEN** by u/{f_by} (in {f_sub})"
            )
        ex = (r.get("exempt_subs") or "").strip()
        if ex:
            exempt_lines.append(f"  - from row in {src}: exempt in {ex}")

    lines = [f"**Audit trail for u/{user}** ({len(rows)} DB row(s))", ""]
    lines.extend(events)
    if exempt_lines:
        lines.append("")
        lines.append("Current exemptions:")
        lines.extend(exempt_lines)

    _reply(
        convo, "\n".join(lines),
        dry_run=dry_run, sub=sub, sender=sender, cmd="history",
    )


def _handle_pardon(convo, user, sub, sender, dry_run, propagate_unban):
    sub_lc = sub.lower()
    rows = database.find_user_records(user)
    matched = next(
        (
            r for r in rows
            if (r.get("source_sub") or "").lower().lstrip("r/").strip("/") == sub_lc
        ),
        None,
    )
    if not matched:
        _reply(
            convo,
            f"⚠️ Cannot pardon u/{user} — they were not banned originally in r/{sub}. "
            "Pardons must come from the origin-sub mod team.",
            dry_run=dry_run, sub=sub, sender=sender, cmd="pardon",
        )
        return

    if (matched.get("manual_override") or "").lower() == "yes":
        _reply(
            convo,
            f"ℹ️ u/{user} was already forgiven. No further action taken.",
            dry_run=dry_run, sub=sub, sender=sender, cmd="pardon",
        )
        return

    source_sub = matched.get("source_sub")
    when = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    if dry_run:
        print(
            f"[DRY-RUN][MODMAIL-PARDON] would pardon u/{user} (source {source_sub}) "
            f"by u/{sender} from r/{sub} and propagate unban"
        )
        return

    if not database.apply_pardon(user, source_sub, sender, sub, when):
        _reply(
            convo,
            f"❌ Internal error pardoning u/{user}; check the bot logs.",
            dry_run=dry_run, sub=sub, sender=sender, cmd="pardon",
        )
        return

    print(
        f"[MODMAIL-PARDON] u/{user} pardoned by u/{sender} from r/{sub} "
        f"(source: {source_sub})"
    )
    log_public_action(
        "FORGIVEN", user, sub, source_sub=source_sub,
        actor=f"{sender} (modmail-pardon)",
    )

    if propagate_unban is not None:
        try:
            propagate_unban(user, source_sub)
        except Exception as e:
            print(f"[MODMAIL-PARDON-WARN] propagation raised: {e}")

    _reply(
        convo,
        f"✅ u/{user} has been forgiven and unbanned across the pact network.",
        dry_run=False, sub=sub, sender=sender, cmd="pardon",
    )


def _handle_exempt(convo, user, sub, sender, dry_run):
    sub_lc = sub.lower()
    rows = database.find_user_records(user)

    if dry_run:
        print(
            f"[DRY-RUN][MODMAIL-EXEMPT] would exempt u/{user} in r/{sub} "
            f"(requested by u/{sender})"
        )
        return

    if not rows:
        # No DB row to attach the exemption to. Be honest about the limitation
        # rather than silently dropping it.
        _reply(
            convo,
            f"ℹ️ u/{user} is not currently in the pact DB, so there's nothing to "
            f"exempt them from. If they get banned via the pact later, "
            f"send `/xsub exempt u/{user}` again.",
            dry_run=False, sub=sub, sender=sender, cmd="exempt",
        )
        return

    updated = 0
    for r in rows:
        if database.add_exemption(r["id"], sub_lc):
            updated += 1

    if updated:
        print(
            f"[MODMAIL-EXEMPT] u/{user} exempted in r/{sub} by u/{sender} "
            f"({updated} row(s) updated)"
        )
        _reply(
            convo,
            f"✅ u/{user} is now exempt from pact bans in r/{sub}.",
            dry_run=False, sub=sub, sender=sender, cmd="exempt",
        )
    else:
        _reply(
            convo,
            f"❌ Failed to record exemption for u/{user}; check the bot logs.",
            dry_run=False, sub=sub, sender=sender, cmd="exempt",
        )


def _reply(convo, body, dry_run, sub, sender, cmd):
    """Reply to a modmail convo, gated by dry_run. Always logs the intent."""
    if dry_run:
        print(f"[DRY-RUN][MODMAIL-REPLY] r/{sub} u/{sender} cmd={cmd}: would reply")
        return
    try:
        convo.reply(body=body)
        print(f"[MODMAIL-REPLY] r/{sub} u/{sender} cmd={cmd}: replied")
    except Exception as e:
        print(f"[MODMAIL-ERROR] r/{sub} u/{sender} cmd={cmd}: reply failed: {e}")
