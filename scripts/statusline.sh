#!/bin/bash
# Claude Code status line for the owner's terminal (Warp over ssh): model, plan usage for 5 hours and the week with
# reset times, context use. Usage comes from the status line input (rate_limits) when Claude Code sends it, otherwise
# from the same cached /usage call as the agents (scripts/agents_usage.sh, at most one call per 2 minutes).
# ~/.claude/settings.json: "statusLine": {"type": "command", "command": "bash ~/tradedesk-mcp/scripts/statusline.sh"}
in="$(cat)"
source "$(dirname "$0")/agents_usage.sh"

usage="$(jq -c '.rate_limits // empty' <<<"$in" 2>/dev/null)"
[[ -z "$usage" ]] && usage="$(usage_json)"

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
  def win(name; w): if w == null or pct(w) == null then empty
    else "\(name) \(pct(w) | floor)%\(when(w))" end;
  [ ($i.model.display_name // empty),
    win("5г"; $u.five_hour),
    win("тиж"; $u.seven_day),
    (if $i.context_window.used_percentage? != null then "ctx \($i.context_window.used_percentage | floor)%" else empty end)
  ] | join(" · ")' 2>/dev/null || echo "Claude"
