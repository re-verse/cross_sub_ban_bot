import json

PROM = "PBFA97CFB590B2093"
LOKI = "P8E80F9AEF21F6940"

def dp(): return {"type": "prometheus", "uid": PROM}
def dl(): return {"type": "loki", "uid": LOKI}

def stat(title, expr, x, y, w=6, h=4, unit="short", color="green"):
    return {"type":"stat","title":title,"datasource":dp(),
        "targets":[{"expr":expr,"refId":"A","datasource":dp(),"instant":True}],
        "fieldConfig":{"defaults":{"unit":unit,"color":{"mode":"fixed","fixedColor":color}},"overrides":[]},
        "options":{"colorMode":"value","graphMode":"area","reduceOptions":{"calcs":["lastNotNull"]}},
        "gridPos":{"x":x,"y":y,"w":w,"h":h}}

def tsp(title, expr, x, y, w=12, h=8, legend="{{source_sub}}", unit="short"):
    return {"type":"timeseries","title":title,"datasource":dp(),
        "targets":[{"expr":expr,"refId":"A","legendFormat":legend,"datasource":dp()}],
        "fieldConfig":{"defaults":{"unit":unit,"custom":{"drawStyle":"line","fillOpacity":10,"showPoints":"never","lineWidth":2}},"overrides":[]},
        "options":{"legend":{"displayMode":"table","placement":"right","calcs":["lastNotNull","max"]},"tooltip":{"mode":"multi"}},
        "gridPos":{"x":x,"y":y,"w":w,"h":h}}

def bars(title, expr, x, y, w=12, h=9):
    return {"type":"barchart","title":title,"datasource":dp(),
        "targets":[{"expr":expr,"refId":"A","legendFormat":"{{source_sub}}","instant":True,"datasource":dp()}],
        "fieldConfig":{"defaults":{"custom":{},"color":{"mode":"palette-classic"}},"overrides":[]},
        "options":{"orientation":"horizontal","legend":{"showLegend":False},"xTickLabelRotation":0,"showValue":"always"},
        "gridPos":{"x":x,"y":y,"w":w,"h":h}}

def pie(title, expr, x, y, w=8, h=9):
    return {"type":"piechart","title":title,"datasource":dp(),
        "targets":[{"expr":expr,"refId":"A","legendFormat":"{{source_sub}}","instant":True,"datasource":dp()}],
        "options":{"legend":{"displayMode":"table","placement":"right","values":["value","percent"]},"pieType":"donut","reduceOptions":{"calcs":["lastNotNull"]}},
        "gridPos":{"x":x,"y":y,"w":w,"h":h}}

def logs(title, expr, x, y, w=24, h=10):
    return {"type":"logs","title":title,"datasource":dl(),
        "targets":[{"expr":expr,"refId":"A","datasource":dl()}],
        "options":{"showTime":True,"wrapLogMessage":True,"sortOrder":"Descending","enableLogDetails":True},
        "gridPos":{"x":x,"y":y,"w":w,"h":h}}

def row(title, y):
    return {"type":"row","title":title,"collapsed":False,"gridPos":{"x":0,"y":y,"w":24,"h":1},"panels":[]}

P=[]; y=0
P.append(row("Pact at a Glance", y)); y+=1
P.append(stat("Total Bans (deduped)", "csbb_bans_total", 0,y,6,4,"short","red"))
P.append(stat("Bans (24h)", "csbb_bans_last_24h", 6,y,6,4,"short","orange"))
P.append(stat("Bans (7d)", "csbb_bans_last_7d", 12,y,6,4,"short","yellow"))
P.append(stat("Forgives (24h)", "csbb_forgives_last_24h", 18,y,6,4,"short","green")); y+=4
P.append(stat("Pact Size", "csbb_trusted_subs", 0,y,6,4,"short","blue"))
P.append(stat("Active Source Subs", "csbb_active_source_subs", 6,y,6,4,"short","purple"))
P.append(stat("Total Forgiven", "csbb_forgiven_total", 12,y,6,4,"short","green"))
P.append(stat("Last Ban Age", "csbb_last_ban_age_seconds", 18,y,6,4,"s","text")); y+=4
P.append(row("Trends (zoom the time-picker for live vs historical)", y)); y+=1
P.append(tsp("Total Bans Over Time", "csbb_bans_total", 0,y,12,8,"total bans","short"))
P.append(tsp("Bans 24h (rolling)", "csbb_bans_last_24h", 12,y,12,8,"24h","short")); y+=8
P.append(tsp("Bans by Source Over Time", "csbb_bans_by_source", 0,y,24,8,"{{source_sub}}","short")); y+=8
P.append(row("Leaderboards (all-time, deduplicated)", y)); y+=1
P.append(bars("Bans by Source Sub", "topk(12, csbb_bans_by_source)", 0,y,12,9))
P.append(pie("Ban Share", "topk(10, csbb_bans_by_source)", 12,y,12,9)); y+=9
P.append(bars("Forgives by Source Sub", "topk(10, csbb_forgives_by_source)", 0,y,12,8))
P.append(bars("Forgiveness Rate by Sub (%)", "100 * csbb_forgives_by_source / (csbb_bans_by_source > 0)", 12,y,12,8)); y+=8
P.append(row("Operational Health", y)); y+=1
P.append(stat("Exporter OK", "csbb_exporter_ok", 0,y,4,4,"bool","green"))
P.append(stat("Host CPU %", "100 - (avg(rate(node_cpu_seconds_total{mode=\"idle\"}[5m]))*100)", 4,y,5,4,"percent","blue"))
P.append(stat("Mem Used %", "100*(1 - node_memory_MemAvailable_bytes/node_memory_MemTotal_bytes)", 9,y,5,4,"percent","blue"))
P.append(stat("Disk /home %", "100*(1 - node_filesystem_avail_bytes{mountpoint=\"/home\"}/node_filesystem_size_bytes{mountpoint=\"/home\"})", 14,y,5,4,"percent","blue"))
P.append(stat("Metrics Fresh", "csbb_last_ban_age_seconds", 19,y,5,4,"s","text")); y+=4
P.append(row("Live Bot Logs (journald via Loki)", y)); y+=1
P.append(logs("csbb.service", '{unit="csbb.service"}', 0,y,24,10)); y+=10

dash={"dashboard":{"uid":"csbb-main","title":"Cross-Sub Ban Pact","tags":["csbb","reddit","bot"],
    "timezone":"browser","schemaVersion":39,"version":0,"refresh":"1m",
    "time":{"from":"now-90d","to":"now"},"panels":P},
    "overwrite":True,"message":"CSBB dashboard v1"}
open("/tmp/csbb-dashboard.json","w").write(json.dumps(dash))
print(f"built {len([p for p in P if p['type']!='row'])} panels + {len([p for p in P if p['type']=='row'])} rows")
