#!/bin/bash
# =====================================================================
# setup-monitoring.sh  —  configure Prometheus + Loki + Alloy + Grafana
# for the cross-sub ban bot on shkn.ws.
#
# SAFE TO READ FIRST. Run as root. Idempotent-ish: backs up any config
# it overwrites to <file>.bak.<timestamp>. Does NOT touch nginx or
# certbot — that's a separate step after we confirm services are up.
#
# Everything binds to 127.0.0.1 — nothing new is exposed to the
# internet by this script. Grafana becomes reachable only later, via
# the nginx vhost step.
# =====================================================================
set -euo pipefail
TS=$(date +%Y%m%d-%H%M%S)
bak() { [ -f "$1" ] && cp -a "$1" "$1.bak.$TS" && echo "  backed up $1 -> $1.bak.$TS" || true; }

echo "=== 1/6  Loki (log store, 127.0.0.1:3100) ==="
bak /etc/loki/config.yml
cat > /etc/loki/config.yml <<'EOF'
# Minimal single-binary Loki, local filesystem storage, localhost only.
auth_enabled: false

server:
  http_listen_address: 127.0.0.1
  http_listen_port: 3100
  grpc_listen_address: 127.0.0.1
  log_level: warn

common:
  instance_addr: 127.0.0.1
  path_prefix: /var/lib/loki
  storage:
    filesystem:
      chunks_directory: /var/lib/loki/chunks
      rules_directory: /var/lib/loki/rules
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory

schema_config:
  configs:
    - from: 2024-01-01
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h

limits_config:
  retention_period: 720h   # 30 days
  reject_old_samples: true
  reject_old_samples_max_age: 168h

compactor:
  working_directory: /var/lib/loki/compactor
  retention_enabled: true
  delete_request_store: filesystem

query_range:
  results_cache:
    cache:
      embedded_cache:
        enabled: true
        max_size_mb: 100

ruler:
  storage:
    type: local
    local:
      directory: /var/lib/loki/rules
EOF
mkdir -p /var/lib/loki/chunks /var/lib/loki/rules /var/lib/loki/compactor
chown -R loki:loki /var/lib/loki 2>/dev/null || chown -R loki /var/lib/loki 2>/dev/null || true
echo "  loki config written"

echo "=== 2/6  Prometheus (metrics store, 127.0.0.1:9090) ==="
bak /etc/prometheus/prometheus.yml
cat > /etc/prometheus/prometheus.yml <<'EOF'
global:
  scrape_interval: 30s
  evaluation_interval: 30s

scrape_configs:
  # Prometheus scrapes itself
  - job_name: prometheus
    static_configs:
      - targets: ['127.0.0.1:9090']

  # Alloy exposes host + integration metrics here
  - job_name: alloy
    static_configs:
      - targets: ['127.0.0.1:12345']

  # Host metrics that Alloy's unix exporter surfaces (via alloy)
  - job_name: node
    static_configs:
      - targets: ['127.0.0.1:12345']
        labels:
          collector: alloy_unix
EOF
# Bind prometheus to localhost only via its EnvironmentFile
bak /etc/default/prometheus
if grep -q '^ARGS=' /etc/default/prometheus 2>/dev/null; then
  sed -i 's|^ARGS=.*|ARGS="--web.listen-address=127.0.0.1:9090 --storage.tsdb.retention.time=90d"|' /etc/default/prometheus
else
  echo 'ARGS="--web.listen-address=127.0.0.1:9090 --storage.tsdb.retention.time=90d"' >> /etc/default/prometheus
fi
echo "  prometheus config written (localhost:9090, 90d retention)"

echo "=== 3/6  Alloy (collector: host metrics + bot journald -> Loki) ==="
bak /etc/alloy/config.alloy
cat > /etc/alloy/config.alloy <<'EOF'
// Alloy: unified collector. Exposes its own metrics on 127.0.0.1:12345,
// scrapes host (unix) metrics, and tails the bot's journald logs to Loki.

logging {
  level = "warn"
}

// ---- Host metrics (replaces node_exporter) ----
prometheus.exporter.unix "host" { }

prometheus.scrape "host" {
  targets    = prometheus.exporter.unix.host.targets
  forward_to = [prometheus.remote_write.local.receiver]
  scrape_interval = "30s"
}

prometheus.remote_write "local" {
  endpoint {
    url = "http://127.0.0.1:9090/api/v1/write"
  }
}

// ---- Logs: tail journald, keep the bot's units, ship to Loki ----
loki.source.journal "journal" {
  forward_to    = [loki.process.bot.receiver]
  labels        = { host = "shkn", source = "journald" }
  relabel_rules = loki.relabel.journal.rules
}

loki.relabel "journal" {
  forward_to = []
  rule {
    source_labels = ["__journal__systemd_unit"]
    target_label  = "unit"
  }
}

loki.process "bot" {
  forward_to = [loki.write.local.receiver]
  // keep everything; the 'unit' label lets us filter csbb in Grafana
}

loki.write "local" {
  endpoint {
    url = "http://127.0.0.1:3100/loki/api/v1/push"
  }
}
EOF
# Alloy must listen on 12345 for Prometheus to scrape it; set in sysconfig
bak /etc/sysconfig/alloy
if grep -q '^CUSTOM_ARGS=' /etc/sysconfig/alloy 2>/dev/null; then
  sed -i 's|^CUSTOM_ARGS=.*|CUSTOM_ARGS="--server.http.listen-addr=127.0.0.1:12345"|' /etc/sysconfig/alloy
else
  echo 'CUSTOM_ARGS="--server.http.listen-addr=127.0.0.1:12345"' >> /etc/sysconfig/alloy
fi
# Alloy runs as user 'alloy'; add to systemd-journal so it can read logs
usermod -aG systemd-journal alloy 2>/dev/null || true
echo "  alloy config written"

echo "=== 4/6  Grafana (127.0.0.1:3000, served later at grafana.shkn.ws) ==="
bak /etc/grafana/grafana.ini
# Targeted edits rather than full rewrite (grafana.ini is huge)
python3 - <<'PYINI'
import re
p = "/etc/grafana/grafana.ini"
s = open(p).read()
def setkv(section, key, val, s):
    # ensure [section] exists, then set/replace key under it
    if f"[{section}]" not in s:
        s += f"\n[{section}]\n{key} = {val}\n"
        return s
    lines = s.split("\n"); out=[]; insec=False; done=False
    for ln in lines:
        st = ln.strip()
        if st.startswith("[") and st.endswith("]"):
            if insec and not done:
                out.append(f"{key} = {val}"); done=True
            insec = (st == f"[{section}]")
        if insec and re.match(rf"^\s*;?\s*{re.escape(key)}\s*=", ln) and not done:
            out.append(f"{key} = {val}"); done=True; continue
        out.append(ln)
    if insec and not done:
        out.append(f"{key} = {val}")
    return "\n".join(out)
s = setkv("server","http_addr","127.0.0.1",s)
s = setkv("server","http_port","3000",s)
s = setkv("server","domain","grafana.shkn.ws",s)
s = setkv("server","root_url","https://grafana.shkn.ws/",s)
open(p,"w").write(s)
print("  grafana.ini: http_addr=127.0.0.1, domain+root_url=grafana.shkn.ws")
PYINI

# Provision datasources so dashboards work immediately
mkdir -p /etc/grafana/provisioning/datasources
bak /etc/grafana/provisioning/datasources/csbb.yaml
cat > /etc/grafana/provisioning/datasources/csbb.yaml <<'EOF'
apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://127.0.0.1:9090
    isDefault: true
  - name: Loki
    type: loki
    access: proxy
    url: http://127.0.0.1:3100
EOF
echo "  grafana datasources provisioned"

echo "=== 5/6  Enable + start services (dependency order) ==="
systemctl daemon-reload
for svc in prometheus loki alloy grafana-server; do
  systemctl enable "$svc" >/dev/null 2>&1 || true
  systemctl restart "$svc"
  sleep 2
  printf "  %-18s %s\n" "$svc" "$(systemctl is-active "$svc")"
done

echo "=== 6/6  Listener check (all should be 127.0.0.1) ==="
ss -tlnp | grep -E ':3000|:9090|:3100|:12345' || echo "  (nothing listening yet — check status below)"

echo
echo "=== DONE. If any service is not 'active', inspect with: ==="
echo "    journalctl -u <service> -n 40 --no-pager"
echo "Next step (separate): nginx vhost + certbot for grafana.shkn.ws"
