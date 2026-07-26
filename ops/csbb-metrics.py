#!/opt/cross_sub_ban_bot/.venv/bin/python
"""
csbb-metrics.py — emit Prometheus metrics for the cross-sub ban pact.

Sources from bans.db (the deduplicated origin-ban table, one row per
unique username+source_sub) rather than the raw public_ban_log.json.
The log counts every propagation event (each origin ban fans out to ~8
target subs, and historically a loop bug inflated some ~150x), so the
DB is the correct source for "how many real bans" questions.
"""
import os
import sqlite3
from collections import Counter
from datetime import datetime, timezone

BOT_DIR = "/opt/cross_sub_ban_bot"
DB = os.path.join(BOT_DIR, "bans.db")
TRUSTED = os.path.join(BOT_DIR, "trusted_subs.txt")
OUT_DIR = "/var/lib/csbb-metrics"
OUT = os.path.join(OUT_DIR, "csbb.prom")
TOP_N = 15


def norm_sub(s):
    return (s or "").strip().lower().lstrip("r/").strip("/") or "unknown"


def parse_ts(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def main():
    now = datetime.now(timezone.utc)
    os.makedirs(OUT_DIR, exist_ok=True)

    try:
        con = sqlite3.connect(DB)
        rows = con.execute(
            "SELECT username, source_sub, timestamp, manual_override, "
            "forgive_timestamp FROM bans"
        ).fetchall()
        con.close()
    except Exception as e:
        with open(OUT, "w") as out:
            out.write("csbb_exporter_ok 0\n")
        print(f"[metrics] failed: {e}")
        return

    bans_by_source = Counter()
    forgives_by_source = Counter()
    bans_24h = bans_7d = forgives_24h = 0
    total_bans = total_forgiven = 0
    newest = None

    for username, source_sub, ts_str, override, f_ts in rows:
        src = norm_sub(source_sub)
        bans_by_source[src] += 1
        total_bans += 1
        ts = parse_ts(ts_str or "")
        if ts:
            if newest is None or ts > newest:
                newest = ts
            age_h = (now - ts).total_seconds() / 3600.0
            if age_h <= 24:
                bans_24h += 1
            if age_h <= 168:
                bans_7d += 1
        if (override or "").lower() == "yes":
            total_forgiven += 1
            forgives_by_source[src] += 1
            fts = parse_ts(f_ts or "")
            if fts and (now - fts).total_seconds() / 3600.0 <= 24:
                forgives_24h += 1

    trusted_n = 0
    try:
        with open(TRUSTED) as f:
            trusted_n = sum(1 for ln in f if ln.strip() and not ln.startswith("#"))
    except Exception:
        pass

    def topn(counter):
        top = counter.most_common(TOP_N)
        other = sum(v for _, v in counter.most_common()[TOP_N:])
        return top, other

    L = []
    def add(name, help_, typ, samples):
        L.append(f"# HELP {name} {help_}")
        L.append(f"# TYPE {name} {typ}")
        L.extend(samples)

    add("csbb_exporter_ok", "1 if exporter ran cleanly", "gauge", ["csbb_exporter_ok 1"])
    add("csbb_bans_total", "total unique origin bans (deduplicated)", "gauge",
        [f"csbb_bans_total {total_bans}"])
    add("csbb_forgiven_total", "origin bans currently forgiven", "gauge",
        [f"csbb_forgiven_total {total_forgiven}"])

    top, other = topn(bans_by_source)
    add("csbb_bans_by_source", "unique origin bans per source sub", "gauge",
        [f'csbb_bans_by_source{{source_sub="{esc(s)}"}} {n}' for s, n in top]
        + [f'csbb_bans_by_source{{source_sub="other"}} {other}'])

    ftop, fother = topn(forgives_by_source)
    add("csbb_forgives_by_source", "forgiven origin bans per source sub", "gauge",
        [f'csbb_forgives_by_source{{source_sub="{esc(s)}"}} {n}' for s, n in ftop]
        + [f'csbb_forgives_by_source{{source_sub="other"}} {fother}'])

    add("csbb_bans_last_24h", "origin bans in last 24h", "gauge",
        [f"csbb_bans_last_24h {bans_24h}"])
    add("csbb_bans_last_7d", "origin bans in last 7d", "gauge",
        [f"csbb_bans_last_7d {bans_7d}"])
    add("csbb_forgives_last_24h", "forgives in last 24h", "gauge",
        [f"csbb_forgives_last_24h {forgives_24h}"])
    add("csbb_active_source_subs", "distinct subs with >=1 ban", "gauge",
        [f"csbb_active_source_subs {len(bans_by_source)}"])
    add("csbb_trusted_subs", "current pact size", "gauge",
        [f"csbb_trusted_subs {trusted_n}"])

    age = int((now - newest).total_seconds()) if newest else -1
    add("csbb_last_ban_age_seconds", "age of most recent ban", "gauge",
        [f"csbb_last_ban_age_seconds {age}"])

    tmp = OUT + ".tmp"
    with open(tmp, "w") as out:
        out.write("\n".join(L) + "\n")
    os.replace(tmp, OUT)
    print(f"[metrics] {total_bans} bans, {total_forgiven} forgiven, 24h={bans_24h}, 7d={bans_7d}")


if __name__ == "__main__":
    main()
