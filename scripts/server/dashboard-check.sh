#!/bin/bash
# From the Mac: the dashboard's data as a short text (for Claude's «перевір агентів»), from the running service's
# /status.json; without the service, builds one snapshot on the server. Reads only.
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" 'cd ~/tradedesk-mcp && { curl -s -m 20 http://127.0.0.1:8090/status.json || python3 -I -c "
import json, sys; sys.path.insert(0, \"scripts\"); import dashboard; print(json.dumps(dashboard.snapshot(), ensure_ascii=False, default=str))"; } | python3 -I -c "
import json, sys, datetime as d
s = json.load(sys.stdin); a = s.get(\"agents\", {}); u = s.get(\"usage\") or {}; m = s.get(\"machine\", {})
print(\"agents:\", a.get(\"line\"), \"|\", a.get(\"activity\"))
print(\"work:\", a.get(\"work\"), \"parked:\", a.get(\"parked\"), \"review:\", a.get(\"awaiting_review\"), \"24h:\", a.get(\"counts_24h\"))
for h in a.get(\"history\", [])[:8]: print(\" \", h[\"when\"], h[\"status\"], h[\"text\"][:110], \"|\", h[\"cost\"][:70])
t = u.get(\"trend\") or {}
f = lambda x: d.datetime.fromtimestamp(x).strftime(\"%d.%m %H:%M\") if x else None
print(\"usage 5h:\", (u.get(\"five_hour\") or {}).get(\"pct\"), \"week:\", (u.get(\"seven_day\") or {}).get(\"pct\"), \"rate/day:\", t.get(\"rate_per_day\"), \"80%:\", f(t.get(\"eta_owner_only\")), \"pause:\", f(t.get(\"eta\")))
print(\"services:\", {x[\"label\"]: x[\"state\"] for x in s.get(\"services\", [])})
print(\"mem GB:\", round((m.get(\"mem_used\") or 0) / 2**30, 1), \"disk %:\", round(100 * m.get(\"disk_used\", 0) / max(1, m.get(\"disk_total\", 1))))
for t in s.get(\"tables\", []): print(\" \", t[\"label\"], \"ok\" if t[\"ok\"] else t[\"ok\"], round((t[\"age_s\"] or 0) / 60), \"min\", t[\"rows\"])
print(\"queue:\", [(c[\"order\"], c[\"name\"][14:], c[\"status\"]) for c in s.get(\"cards\", []) if c[\"status\"] in (\"accepted\", \"testing\", \"proposed\")])
print(\"questions:\", s.get(\"questions\"))
"'
