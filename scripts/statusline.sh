#!/bin/bash
# Claude Code status line for the owner's terminal (Warp over ssh): model, plan usage for 5 hours and the week with
# reset times, context use; each as a coloured bar (green to 50 %, yellow to 80 %, red above). Usage comes from the
# same cached /usage call as the agents (scripts/agents_usage.sh, at most one call per 2 minutes), falling back to
# the status line input (rate_limits) when that call fails.
# ~/.claude/settings.json: "statusLine": {"type": "command", "command": "bash ~/tradedesk-mcp/scripts/statusline.sh"}
in="$(cat)"
source "$(dirname "$0")/agents_usage.sh"

# The /usage figures first (Claude Code's own rate_limits lagged them by ~1 point, 2026-10-19), its input as fallback.
usage="$(usage_json)"
[[ -z "$usage" ]] && usage="$(jq -c '.rate_limits // empty' <<<"$in" 2>/dev/null)"

[[ -z "$in" ]] && in='{}'
[[ -z "$usage" ]] && usage='{}'
jq -rn --argjson i "$in" --argjson u "$usage" '
  def pct(w): (w.used_percentage // w.utilization);
  def when(w): (w.resets_at // null) as $r
    | if $r == null then ""
      else (if ($r | type) == "number" then $r else ($r | sub("\\.[0-9]+"; "") | sub("\\+00:00$"; "Z") | fromdateiso8601) end)
        | . as $e | " ↻" + (if ($e | localtime | strftime("%F")) == (now | localtime | strftime("%F"))
            then ($e | localtime | strftime("%H:%M"))
            else (["нд","пн","вт","ср","чт","пт","сб"][$e | localtime | strftime("%w") | tonumber]) + " " + ($e | localtime | strftime("%H:%M")) end)
      end;
  # A 10-cell bar and the figure, green up to 50 %, yellow 51-80 %, red above.
  def gauge(p): (p | floor) as $p | ([([$p / 10 | ceil, 10] | min), 0] | max) as $n
    | (if $p <= 50 then "32" elif $p <= 80 then "33" else "31" end) as $c
    | "\u001b[\($c)m" + ([range($n)] | map("▰") | join("")) + "\u001b[2m" + ([range(10 - $n)] | map("▱") | join(""))
      + "\u001b[22m \($p)%\u001b[0m";
  def win(name; w): if w == null or pct(w) == null then empty
    else "\(name) \(gauge(pct(w)))\(when(w))" end;
  [ ($i.model.display_name // empty),
    win("5г"; $u.five_hour),
    win("тиж"; $u.seven_day),
    (if $i.context_window.used_percentage? != null then "ctx \(gauge($i.context_window.used_percentage))" else empty end)
  ] | join(" · ")' 2>/dev/null || echo "Claude"
