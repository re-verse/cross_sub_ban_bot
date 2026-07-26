"""
Modmail command handler for the Cross-Sub Ban Pact bot.

Listens for these commands as the latest mod-authored message in a
modmail thread on each trusted sub — scanning both incoming
conversations (state="new") and Mod Discussions (state="mod", where a
mod messaging their own sub lands):

  /xsub help                  -> static reference card
  /xsub status u/<username>   -> facts from the bans DB (any trusted-sub mod)
  /xsub pardon u/<username>   -> forgive + propagate unban (origin-sub mods only)
  /xsub exempt u/<username>   -> exempt user from bans in this sub only

Behaviour notes:
- The bot replies at most once per modmail conversation: any thread the
  bot has ever replied in is skipped on later scans. To issue another
  command, start a new modmail thread. (Deliberate — prevents reply
  loops at the cost of no in-thread follow-ups.)
- /xsub pardon also propagates the unban across the network. The
  pre-Sheets-migration version only flipped the override flag, which
  left users still banned on every other sub. Mods reasonably expect
  pardon to mean "undo + prevent re-ban".
- Modmail iteration failures are caught per-sub and recorded against
  the health tracker so they surface alongside modlog access drift.
"""
from datetime import datetime
from bot_config import database, reddit, TRUSTED_SUBS, OWNER_USERNAME, CROSS_SUB_BAN_REASON
from core_utils import is_mod
from log_utils import log_public_action
from subreddit_discovery import (
    load_trusted, save_trusted, load_pending, save_pending,
    approve_sub, decline_sub, remove_sub,
)
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
    "    /xsub help                       — this message\n"
    "    /xsub status u/username          — show this user's status across the network\n"
    "    /xsub history u/username         — chronological audit trail for this user\n"
    "    /xsub pardon u/username          — forgive + unban a user (origin-sub mods only)\n"
    "    /xsub exempt u/username          — exempt this user from bans in your sub only\n"
    "    /xsub super ban u/username ...   — manual cross-sub ban (bot owner only)\n"
    "    /xsub approve r/subname          — move sub from pending to trusted (owner only)\n"
    "    /xsub decline r/subname          — drop pending sub without trusting (owner only)\n"
    "    /xsub remove r/subname           — remove sub from trusted list (owner only)\n\n"
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


def check_modmail(health_state=None, dry_run=False, propagate_unban=None, propagate_ban=None):
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
    propagate_ban: optional callable(username, source_sub, actor, note)
        used by /xsub super ban to apply the ban across the entire
        trusted-sub network. Owner-only command.
    """
    print("[MODMAIL] Checking for /xsub commands...")
    bot_name = _bot_username()

    # Two folders matter:
    #   state="new" — incoming conversations from users (a mod of sub A
    #                 messaging sub B lands here, as a regular user of B)
    #   state="mod" — Mod Discussions (a mod messaging THEIR OWN sub
    #                 lands here — which is the primary designed usage)
    # Scanning only "new" left the main use case dark: commands from a
    # sub's own mods were never seen. Verified live 2026-07-23.
    _SCAN_STATES = ("new", "mod")

    for sub in TRUSTED_SUBS:
        try:
            sr = reddit.subreddit(sub)
            seen = 0
            seen_convo_ids = set()
            for state in _SCAN_STATES:
                if seen > _MAX_CONVOS_PER_SUB:
                    break
                for convo in sr.modmail.conversations(state=state):
                    cid = getattr(convo, "id", None)
                    if cid is not None and cid in seen_convo_ids:
                        continue
                    if cid is not None:
                        seen_convo_ids.add(cid)
                    seen += 1
                    if seen > _MAX_CONVOS_PER_SUB:
                        break
                    _handle_convo(
                        convo, sr, sub, bot_name,
                        dry_run=dry_run,
                        propagate_unban=propagate_unban,
                        propagate_ban=propagate_ban,
                    )
        except prawcore.exceptions.Forbidden as e:
            # 403 here means the sub granted the bot 'access' (ban power,
            # which is all the pact REQUIRES) but not 'mail'. That is a
            # deliberate configuration choice, not a fault: ban
            # propagation works perfectly without it. The only thing lost
            # is that this sub's mods can't drive /xsub commands from
            # their own modmail.
            #
            # So this is NOT recorded as a health failure — recording it
            # would show the sub red on the dashboard and fire recurring
            # alerts about something that isn't broken. If they later
            # grant 'mail', polling simply starts working again on the
            # next run with no state to clear.
            print(f"[MODMAIL-SKIP] r/{sub} has not granted 'mail' "
                  f"permission - skipping modmail (ban propagation "
                  f"unaffected)")
        except prawcore.exceptions.NotFound as e:
            print(f"[MODMAIL-WARN] NotFound on r/{sub}: {e}")
            if health_state is not None:
                from health_utils import record_failure
                record_failure(health_state, f"{sub}:modmail", e)
        except Exception as e:
            print(f"[MODMAIL-ERROR] r/{sub}: {e}")


def _handle_convo(convo, sr, sub, bot_name, dry_run=False, propagate_unban=None, propagate_ban=None):
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

    # /xsub super <subcmd> u/user [reason...]  — owner-only.
    # Handled separately because the argument layout differs from the
    # other commands (subcommand at parts[2], not a username).
    if cmd == "super":
        _handle_super(
            convo, parts, sub, sender,
            dry_run=dry_run, propagate_ban=propagate_ban,
        )
        return

    # Network management commands (owner-only) — work via both DM and
    # modmail. Argument is r/subname, not a username.
    if cmd in ("approve", "decline", "remove"):
        _handle_management(convo, cmd, parts, sub, sender, dry_run=dry_run)
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
        actor = (r.get("moderator_name") or "").strip()
        by = f" by u/{actor}" if actor else ""
        events.append(
            f"- `{ts}`  **BANNED** in {src}{by}"
        )
        if (r.get("manual_override") or "").lower() == "yes":
            f_ts = r.get("forgive_timestamp") or "?"
            f_sub = r.get("mod_sub") or "?"
            events.append(
                f"- `{f_ts}`  **FORGIVEN** (via {f_sub})"
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
            f"via r/{sub} modmail and propagate unban"
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
        f"[MODMAIL-PARDON] u/{user} pardoned via r/{sub} modmail "
        f"(source: {source_sub})"
    )
    log_public_action(
        "FORGIVEN", user, sub, source_sub=source_sub,
        actor="origin-sub mods (pardon)",
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
            f"(requested via r/{sub} modmail)"
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
            f"[MODMAIL-EXEMPT] u/{user} exempted in r/{sub} "
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
        print(f"[DRY-RUN][MODMAIL-REPLY] r/{sub} cmd={cmd}: would reply")
        return
    try:
        convo.reply(body=body)
        print(f"[MODMAIL-REPLY] r/{sub} cmd={cmd}: replied")
    except Exception as e:
        print(f"[MODMAIL-ERROR] r/{sub} cmd={cmd}: reply failed: {e}")


def _handle_super(convo, parts, sub, sender, dry_run, propagate_ban):
    """
    Owner-only /xsub super <subcmd> handler.

    Current subcommands:
      ban u/username [reason...]  -> apply pact ban across ALL trusted subs
                                     with source_sub='manual' so it doesn't
                                     get re-processed by modlog propagation.

    Only the configured OWNER_USERNAME can run super commands. Any other
    mod gets a flat refusal. We don't silently drop because the sender
    already passed is_mod for this sub — they deserve to know why.
    """
    # parts = ['/xsub', 'super', '<subcmd>', '<user>', 'reason', ...]
    if sender != OWNER_USERNAME.lower():
        _reply(
            convo,
            f"❌ `/xsub super` commands are restricted to u/{OWNER_USERNAME}.",
            dry_run=dry_run, sub=sub, sender=sender, cmd="super",
        )
        return

    if len(parts) < 4:
        _reply(
            convo,
            "⚠️ Format: `/xsub super ban u/username <reason>`",
            dry_run=dry_run, sub=sub, sender=sender, cmd="super",
        )
        return

    subcmd = parts[2]
    user = _parse_username(parts[3])
    reason = " ".join(parts[4:]).strip() or "Manual cross-sub ban (no reason given)"

    if not user:
        _reply(
            convo,
            "⚠️ Could not parse the username. Format: `/xsub super ban u/username <reason>`",
            dry_run=dry_run, sub=sub, sender=sender, cmd="super",
        )
        return

    if subcmd != "ban":
        _reply(
            convo,
            f"⚠️ Unknown super subcommand `{subcmd}`. Only `ban` is supported.",
            dry_run=dry_run, sub=sub, sender=sender, cmd="super",
        )
        return

    _handle_super_ban(
        convo, user, reason, sub, sender,
        dry_run=dry_run, propagate_ban=propagate_ban,
    )


def _handle_super_ban(convo, user, reason, sub, sender, dry_run, propagate_ban):
    """
    Apply a manual cross-sub ban initiated by the owner.

    Uses source_sub='manual' for two reasons:
    1. It signals in the public log that this didn't originate from any
       trusted sub's modlog.
    2. apply_ban_across_network's skip-the-source-sub clause becomes a
       no-op, so all 9 subs get the ban.

    The DB row is inserted so the same user can't be super-banned twice
    by accident, and so /xsub status / /xsub history surface the action.
    """
    if propagate_ban is None:
        _reply(
            convo,
            "❌ Internal error: super-ban not wired to a propagation handler.",
            dry_run=dry_run, sub=sub, sender=sender, cmd="super",
        )
        return

    when = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    log_id = f"supermodmail_{when.replace(' ', 'T')}_{user}"
    note = (
        "Manual cross-sub ban applied by the pact operators via modmail. "
        f"Reason: {reason}. NHL subs share a pact to fight trolling. "
        f"To appeal, message mods of any participating sub."
    )

    if dry_run:
        print(
            f"[DRY-RUN][MODMAIL-SUPER-BAN] would super-ban u/{user} across all "
            f"{len(TRUSTED_SUBS)} trusted subs (reason: {reason})"
        )
        # Still call propagate_ban so the dry-run log shows the per-sub intent
        propagate_ban(
            user, "manual",
            actor="pact-owner (super-ban)",
            note=note,
        )
        return

    # Insert the DB row first so a partial Reddit failure still leaves a
    # record. append_row is silently idempotent on (username, source_sub)
    # unique constraint — repeat super-bans of the same user are no-ops.
    row_data = [
        user,
        "manual",
        CROSS_SUB_BAN_REASON,
        when,
        '',  # manual_override blank — this is a fresh ban, not forgiven
        log_id,
        sender,
    ]
    try:
        database.append_row(row_data)
    except Exception as e:
        print(f"[MODMAIL-SUPER-BAN-WARN] DB insert failed (may be duplicate): {e}")

    # Public log gets one top-level row too, so the audit trail clearly
    # shows the SUPER-BAN action was initiated, not just the propagation.
    log_public_action(
        "BANNED", user, "manual", source_sub="manual",
        actor="pact-owner (super-ban)",
        note=reason,
    )

    print(
        f"[MODMAIL-SUPER-BAN] u/{user} super-banned by owner (reason: {reason})"
    )

    try:
        propagate_ban(
            user, "manual",
            actor="pact-owner (super-ban)",
            note=note,
        )
    except Exception as e:
        print(f"[MODMAIL-SUPER-BAN-WARN] propagation raised: {e}")
        _reply(
            convo,
            f"⚠️ u/{user} ban initiated but propagation raised an error — check logs.",
            dry_run=False, sub=sub, sender=sender, cmd="super",
        )
        return

    _reply(
        convo,
        f"✅ u/{user} has been banned across the pact network ({len(TRUSTED_SUBS)} subs attempted).\n\n"
        f"Reason: {reason}",
        dry_run=False, sub=sub, sender=sender, cmd="super",
    )


def _handle_management(convo, cmd, parts, sub, sender, dry_run):
    """
    Owner-only modmail handler for /xsub approve | decline | remove.

    Argument is r/subname, not a username. Mirrors the DM-side
    handler in inbox_utils so both surfaces accept the commands.
    """
    if sender != OWNER_USERNAME.lower():
        _reply(
            convo,
            f"❌ `/xsub {cmd}` is restricted to u/{OWNER_USERNAME}.",
            dry_run=dry_run, sub=sub, sender=sender, cmd=cmd,
        )
        return

    if len(parts) < 3:
        _reply(
            convo,
            f"⚠️ Format: `/xsub {cmd} r/<subname>`",
            dry_run=dry_run, sub=sub, sender=sender, cmd=cmd,
        )
        return

    target = parts[2]

    if dry_run:
        print(
            f"[DRY-RUN][MODMAIL-MGMT] would `/xsub {cmd} {target}` "
            f"(via r/{sub})"
        )
        return

    try:
        trusted = load_trusted()
        pending = load_pending()
        if cmd == "approve":
            ok, msg = approve_sub(target, trusted, pending)
        elif cmd == "decline":
            ok, msg = decline_sub(target, pending)
        elif cmd == "remove":
            ok, msg = remove_sub(target, trusted, pending)
        else:
            _reply(convo, f"⚠️ Unknown management cmd `{cmd}`.",
                   dry_run=False, sub=sub, sender=sender, cmd=cmd)
            return

        if ok:
            save_trusted(trusted)
            save_pending(pending)
            print(f"[MODMAIL-MGMT] u/{sender} {cmd} {target}: {msg}")
            _reply(
                convo,
                f"✅ {msg}. Takes effect on the next cron tick (~20 min).",
                dry_run=False, sub=sub, sender=sender, cmd=cmd,
            )
        else:
            _reply(
                convo,
                f"⚠️ {msg}.",
                dry_run=False, sub=sub, sender=sender, cmd=cmd,
            )
    except Exception as e:
        print(f"[MODMAIL-MGMT-ERROR] cmd={cmd} target={target}: {e}")
        _reply(
            convo,
            f"❌ Internal error processing `/xsub {cmd} {target}` — check logs.",
            dry_run=False, sub=sub, sender=sender, cmd=cmd,
        )
