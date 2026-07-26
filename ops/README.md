# Server Ops (`ops/`)

Everything needed to run the bot on a dedicated server (currently
`shkn.ws`) instead of / alongside GitHub Actions. Versioned here so the
setup is reproducible if the box is ever rebuilt.

## Architecture

The bot runs **primarily on the server** via a systemd timer, and
**GitHub Actions is the automatic backup**. Both run identical code
from this repo and coordinate through git-committed state on `main`:

- Server runs every N min (`csbb.timer`), commits state to `main` as
  `csbb-bot (shkn)`, pushes.
- GitHub's scheduled run checks the last `csbb-bot (shkn)` commit age;
  if recent, it stands down (see the failover guard in
  `.github/workflows/run_bot.yml`). If the server goes silent, GitHub
  takes over within one cycle. `workflow_dispatch` is break-glass.

Because both write to the same git-backed state, they can't diverge.

## Files

| File | Installed to | Purpose |
|------|-------------|---------|
| `run-and-push.sh` | `/opt/csbb-ops/` | systemd ExecStart: runs bot, refreshes metrics, commits + pushes state with retry backoff |
| `csbb-metrics.py` | `/opt/csbb-ops/` | reads `bans.db`, writes Prometheus textfile to `/var/lib/csbb-metrics/csbb.prom` |
| `backfill_bans_db.py` | run once | rebuilds `bans.db` from `public_ban_log.json`, deduped to unique (user, source_sub) origin bans |
| `systemd/csbb.service` | `/etc/systemd/system/` | oneshot unit, runs as `re-verse`, single py3.12 version guard |
| `systemd/csbb.timer` | `/etc/systemd/system/` | schedule |
| `monitoring/setup-monitoring.sh` | run once | configures Prometheus/Loki/Alloy/Grafana |
| `monitoring/config.alloy` | `/etc/alloy/` | Alloy collector: host metrics + journald→Loki + csbb textfile |

## Secrets (NOT in repo)

`/etc/csbb/csbb.env` (root:re-verse, 640) holds the 4 Reddit creds +
`HEALTHCHECK_URL`. Same values as the GitHub Actions repo secrets.

## Rebuild checklist (if the box dies)

1. Rocky 9, install `python3.12`, `git`, `nginx`, `certbot`.
2. Deploy key: generate ed25519, register as repo deploy key (write),
   add `github-csbb` SSH host alias.
3. Clone to `/opt/cross_sub_ban_bot`, build `.venv` on python3.12,
   `pip install -r requirements.txt`.
4. Create `/etc/csbb/csbb.env` with the creds.
5. Copy `ops/` scripts to `/opt/csbb-ops/`, systemd units to
   `/etc/systemd/system/`, `daemon-reload`.
6. `backfill_bans_db.py` if `bans.db` is empty.
7. `monitoring/setup-monitoring.sh` for observability, nginx vhost +
   certbot for `grafana.shkn.ws`.
8. `systemctl enable --now csbb.timer`.

## Monitoring

- Grafana: `https://grafana.shkn.ws` (admin pw in server `/root/grafpass`)
- Prometheus (`127.0.0.1:9090`), Loki (`127.0.0.1:3100`), Alloy
  (`127.0.0.1:12345`) — all localhost, Grafana reverse-proxied via nginx+TLS.
- Healthchecks.io dead-man's switch pinged by whichever executor runs.
