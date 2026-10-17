#!/bin/bash
# From the Mac: the latest GitHub Actions runs of the repo (workflow, branch, result, commit) with the server's token,
# and for a failed run its failed jobs and steps. Prints results only, never the token. Reads only.
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" 'python3 -I - <<"PY"
import json, urllib.request, pathlib
tok = ""
for line in (pathlib.Path.home() / ".config/tradedesk/github.env").read_text().splitlines():
    if line.startswith("GITHUB_TOKEN="):
        tok = line.split("=", 1)[1].strip()
def get(path):
    req = urllib.request.Request("https://api.github.com/repos/nick198922/MCP" + path,
                                 headers={"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except Exception as e:
        return {"error": str(e)}
pulls = get("/pulls?state=open&per_page=20")
for pr in (pulls if isinstance(pulls, list) else []):
    print("open PR #%d" % pr["number"], pr["head"]["ref"], pr["created_at"][5:16], "draft" if pr.get("draft") else "")
runs = get("/actions/runs?per_page=15")
if "error" in runs:
    print("runs:", runs["error"]); raise SystemExit
for r in runs.get("workflow_runs", []):
    print(r["created_at"][5:16], r["name"], r["event"], r["head_branch"][:40], r["status"], r["conclusion"], r["head_sha"][:7], r["display_title"][:60])
failed = [r for r in runs.get("workflow_runs", []) if r["conclusion"] == "failure"][:2]
for r in failed:
    jobs = get("/actions/runs/%d/jobs" % r["id"])
    for j in jobs.get("jobs", []):
        if j.get("conclusion") == "failure":
            steps = [s["name"] for s in j.get("steps", []) if s.get("conclusion") == "failure"]
            print("FAILED", r["name"], r["head_sha"][:7], "job:", j["name"], "steps:", steps)
PY'
