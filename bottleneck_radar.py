#!/usr/bin/env python3
"""
Bottleneck Radar
================
Data-driven bottleneck detection for GitHub projects.

Analyzes a repository for:
  * aged issue backlog & staleness
  * label distribution (where work piles up)
  * pull-request queue health
  * contributor concentration (bus factor)
  * release cadence vs. development activity
  * translation/asset string queues (optional file scan)

Outputs a Markdown report (German) + JSON.
Design principle: AI orchestrates, humans decide.

Usage:
  python3 bottleneck_radar.py --owner <owner> --repo <name> \
      --token <ghp_...> \
      [--out report.md] [--json report.json] \
      [--string-file path/to/queue.json]... \
      [--daily-rate 50] [--stale-days 14]

The token is only needed for rate limits (public repos work without).
Never commit tokens or reports containing private data.
"""

from __future__ import annotations

import argparse
import base64
import json
import statistics
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.github.com"
UA = "bottleneck-radar"


# --------------------------------------------------------------------------
# GitHub API helpers (stdlib only — no dependencies)
# --------------------------------------------------------------------------

def api(url: str, token: str | None) -> object:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": UA,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def api_paginated(url: str, token: str | None, per_page: int = 100,
                  max_pages: int = 10) -> list:
    items: list = []
    for page in range(1, max_pages + 1):
        sep = "&" if "?" in url else "?"
        batch = api(f"{url}{sep}per_page={per_page}&page={page}", token)
        if not batch:
            break
        items.extend(batch)
        if len(batch) < per_page:
            break
    return items


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def age_days(ts: datetime | None, now: datetime) -> float | None:
    if ts is None:
        return None
    return max(0.0, (now - ts).total_seconds() / 86400.0)


def fmt_days(d: float | None) -> str:
    if d is None:
        return "–"
    return f"{d:.0f} T" if d < 60 else f"{d / 30.0:.1f} M"


def file_entry_count(url: str, token: str | None) -> int | None:
    """Count top-level entries of a JSON file in the repo (metadata only)."""
    try:
        data = api(url, token)
    except (urllib.error.HTTPError, urllib.error.URLError):
        return None
    raw = data.get("content")
    if not raw:
        return None
    try:
        parsed = json.loads(base64.b64decode(raw))
    except Exception:
        return None
    if isinstance(parsed, list):
        return len(parsed)
    if isinstance(parsed, dict):
        # largest list or object inside = likely the queue
        sizes = [len(v) for v in parsed.values()
                 if isinstance(v, (list, dict))]
        return max(sizes) if sizes else len(parsed)
    return None


# --------------------------------------------------------------------------
# Data collection
# --------------------------------------------------------------------------

def collect(owner: str, repo: str, token: str | None,
            string_files: list[str]) -> dict:
    out: dict = {}

    out["repo"] = api(f"{API}/repos/{owner}/{repo}", token)

    items = api_paginated(
        f"{API}/repos/{owner}/{repo}/issues?state=open", token)
    out["issues"] = [i for i in items if "pull_request" not in i]
    out["pulls"] = [i for i in items if "pull_request" in i]

    out["contributors"] = api(
        f"{API}/repos/{owner}/{repo}/contributors?per_page=100", token)
    out["releases"] = api(
        f"{API}/repos/{owner}/{repo}/releases?per_page=10", token)
    out["commits"] = api(
        f"{API}/repos/{owner}/{repo}/commits?per_page=100", token)

    strings = []
    for path in string_files:
        cnt = file_entry_count(
            f"{API}/repos/{owner}/{repo}/contents/{path}?ref="
            f"{out['repo'].get('default_branch', 'main')}", token)
        strings.append({"path": path, "entries": cnt})
    out["string_files"] = strings

    return out


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

def analyze(data: dict, daily_rate: int, stale_days: int) -> dict:
    now = datetime.now(timezone.utc)
    issues = data["issues"]
    pulls = data["pulls"]
    contributors = data.get("contributors") or []
    releases = data.get("releases") or []
    commits = data.get("commits") or []

    # --- backlog & aging
    created = [parse_ts(i["created_at"]) for i in issues]
    created = [c for c in created if c]
    ages = sorted(age_days(c, now) or 0 for c in created)
    oldest = max(ages) if ages else 0
    median = statistics.median(ages) if ages else 0
    stale = [i for i in issues
             if age_days(parse_ts(i.get("updated_at")), now)
             and age_days(parse_ts(i["updated_at"]), now) >= stale_days]

    # --- labels
    label_counts: dict[str, int] = {}
    for i in issues:
        for lab in i.get("labels", []):
            name = lab.get("name", "") if isinstance(lab, dict) else str(lab)
            label_counts[name or "(unlabeled)"] = (
                label_counts.get(name or "(unlabeled)", 0) + 1)
    labels = sorted(label_counts.items(), key=lambda kv: -kv[1])

    # --- PRs
    pr_info = []
    for p in pulls:
        pr_info.append({
            "number": p["number"],
            "title": p["title"][:80],
            "age_days": age_days(parse_ts(p["created_at"]), now),
            "draft": bool(p.get("pull_request", {}).get("draft")),
        })

    # --- bus factor
    total_commits = sum(c.get("contributions", 0) for c in contributors)
    top1 = contributors[0] if contributors else None
    top2 = contributors[1] if len(contributors) > 1 else None
    top1_share = (top1["contributions"] / total_commits * 100
                  if top1 and total_commits else None)
    top2_share = (top2["contributions"] / total_commits * 100
                  if top2 and total_commits else None)

    # --- releases & activity
    last_commit = max((parse_ts(c["commit"]["author"]["date"])
                       for c in commits if c.get("commit", {})
                       .get("author", {}).get("date")), default=None)
    last_release = None
    for r in releases:
        if r.get("published_at"):
            t = parse_ts(r["published_at"])
            if t and (last_release is None or t > last_release):
                last_release = t

    weeks = []
    for w in range(7, -1, -1):
        start = now - timedelta(weeks=w + 1)
        end = now - timedelta(weeks=w)
        wc = sum(1 for c in commits
                 if (t := parse_ts(c["commit"]["author"]["date"]))
                 and start <= t < end)
        wi = sum(1 for i in issues
                 if (t := parse_ts(i["created_at"])) and start <= t < end)
        weeks.append({"week": (end - timedelta(days=3)).strftime("%Y-%m-%d"),
                      "commits": wc, "issues": wi})

    # --- string queues
    strings = []
    for sf in data.get("string_files", []):
        entries = sf.get("entries")
        strings.append({
            **sf,
            "estimated_days": round(entries / daily_rate)
            if entries else None,
        })

    # --- findings (severity: 3 = critical, 2 = warning, 1 = info)
    findings: list[dict] = []

    def add(sev, title, detail, rec):
        findings.append({"severity": sev, "title": title,
                         "detail": detail, "recommendation": rec})

    if top1 and top1_share is not None and top1_share >= 99:
        add(3, "Bus-Faktor 1",
            f"Ein einziger Contributor ({top1['login']}) hat 100% der "
            f"Commits.",
            "Wissen ist bei einer Person konzentriert. Dokumentation und "
            "Werkzeuge bauen; zweite Person mit Write-Zugriff einbinden, "
            "um die Abhängigkeit zu brechen.")
    elif top1_share is not None and top1_share >= 70:
        add(2, "Bus-Faktor niedrig",
            f"Top-Contributor {top1['login']} hat {top1_share:.0f}% der "
            f"Commits ({top1['contributions']}/{total_commits}).",
            "Code-Reviews verteilen; Pair-Programmierung/Handoffs "
            "dokumentieren, um Wissen zu teilen.")

    if ages and oldest >= 21:
        oldest_issue = max(issues, key=lambda i: i["created_at"] == min(
            [x["created_at"] for x in issues]))
        add(2, "Alter Backlog",
            f"Ältestes offenes Issue liegt seit {fmt_days(oldest)} im "
            f"Backlog (Median {fmt_days(median)}).",
            "Älteste Issues in einem Triage-Meeting klären: beenden, "
            "planen oder schließen. Alter Backlog verfängt sich im "
            "Projekt.")
    if stale:
        add(1 if len(stale) <= 2 else 2,
            f"{len(stale)} Issues veraltet (> {stale_days} Tage inaktiv)",
            "Aktiv: " + ", ".join(f"#{i['number']}" for i in stale[:8])
            + (" …" if len(stale) > 8 else ""),
            "Veraltete Issues entweder voranbringen, mit Label "
            "'stale' kennzeichnen oder zu Issues mit klarem "
            "Nächsten-Schritt umformulieren.")

    if last_commit is None:
        pass
    else:
        dc = age_days(last_commit, now)
        if dc is not None and dc > 7:
            add(2, "Aktivität lässt nach",
                f"Letzter Commit vor {fmt_days(dc)}.",
                "Kleine, abgeschlossene PRs anstoßen, um den Rhythmus "
                "wieder einzufangen (z. B. Doku- oder Tooling-PRs).")

    if last_release and last_commit and last_commit > last_release:
        commits_since = sum(
            1 for c in commits
            if (t := parse_ts(c["commit"]["author"]["date"]))
            and t > last_release)
        dr = age_days(last_release, now)
        if dr is not None and dr > 30:
            add(2, "Release-Lücke",
                f"Letztes Release vor {fmt_days(dr)}, danach "
                f"{commits_since} Commits.",
                "Release-Checkliste auslagern und automatisieren "
                "(Checksummen, Changelog), damit Commits nicht "
                "unveröffentlicht hängen bleiben.")
    if not releases:
        add(1, "Keine Releases erfasst",
            "Das Repo hat keine GitHub Releases.",
            "Releases für Versionierung und MD5-Prüfsummen nutzen.")

    if pr_info:
        old_prs = [p for p in pr_info if (p["age_days"] or 0) >= stale_days]
        if old_prs:
            add(2, "PR-Warteschlange",
                "Offene PRs: " + ", ".join(
                    f"#{p['number']} ({fmt_days(p['age_days'])})"
                    for p in old_prs[:6]),
                "PRs im Triage-Kanal besprechen; Review-Fenster oder "
                "Auto-Review durch AI-Codereview einführen.")

    for sf in strings:
        if sf.get("entries") and sf["entries"] >= 500:
            add(2, f"String-Queue: {sf['entries']:,} Einträge",
                f"{sf['path']} enthält {sf['entries']:,} Einträge; bei "
                f"{daily_rate}/Tag ≈ {sf['estimated_days']} Tage "
                f"menschlicher Durchlauf.",
                "Durchsatz erhöhen: AI-Vorfilterung (Terminologie, "
                "Textbox-Fit), Fuzzy-Matching gegen Bestand, dann nur "
                "Diffs von Menschen prüfen.")

    if not findings:
        add(1, "Keine kritischen Flaschenhälse erkannt",
            "Alle gemessenen Kennzahlen sind im grünen Bereich.",
            "Kennzahlen regelmäßig (z. B. wöchentlich) neu messen, "
            "um Drift früh zu erkennen.")

    findings.sort(key=lambda f: -f["severity"])

    return {
        "generated_at": now.isoformat(),
        "backlog": {
            "open": len(issues),
            "oldest_days": round(oldest, 1),
            "median_days": round(median, 1),
            "stale": [{"number": i["number"], "title": i["title"][:80]}
                      for i in stale],
        },
        "labels": labels,
        "prs": pr_info,
        "bus_factor": {
            "contributors": [
                {"login": c["login"], "contributions": c["contributions"]}
                for c in contributors[:10]],
            "total_commits_sampled": total_commits,
            "top1_share_pct": round(top1_share, 1) if top1_share else None,
        },
        "activity": {
            "last_commit": str(last_commit),
            "last_release": str(last_release),
            "weeks": weeks,
        },
        "string_queues": strings,
        "findings": findings,
    }


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

SEV_ICON = {3: "🔴", 2: "🟠", 1: "🟢"}


def render_report(owner: str, repo: str, res: dict) -> str:
    repo_obj = res["repo"]
    a = res["analysis"]
    L: list[str] = []
    add = L.append

    add(f"# 📡 Bottleneck Radar — {owner}/{repo}")
    add("")
    add(f"> Stand: {a['generated_at'][:10]} · "
        f"{'privat' if repo_obj.get('private') else 'öffentlich'} · "
        f"{repo_obj.get('language') or '—'} · Default Branch "
        f"`{repo_obj.get('default_branch')}`")
    add("")
    add("## 🎯 Top-Flaschenhälse (nach Schweregrad)")
    add("")
    if not a["findings"]:
        add("_Keine Messungen durchgeführt._")
    add("| # | Schwere | Befund | Details | Empfehlung |")
    add("|---|---------|--------|---------|------------|")
    for n, f in enumerate(a["findings"], 1):
        add(f"| {n} | {SEV_ICON.get(f['severity'], '·')} "
            f"{f['title']} | {f['detail']} | {f['recommendation']} |")
    add("")

    add("## 📊 Backlog (offene Issues)")
    add("")
    add(f"- Offen: **{a['backlog']['open']}** · "
        f"ältestes: **{fmt_days(a['backlog']['oldest_days'])}** · "
        f"Median: **{fmt_days(a['backlog']['median_days'])}**")
    if a["backlog"]["stale"]:
        add(f"- Veraltet (≥ 14 Tage inaktiv): "
            + ", ".join(f"#{s['number']}" for s in a["backlog"]["stale"]))
    add("")
    if a["labels"]:
        add("**Verteilung nach Labels:** "
            + " · ".join(f"`{k}` ({v})" for k, v in a["labels"]))
        add("")

    if a["prs"]:
        add("## 🔄 Offene Pull Requests")
        add("")
        add("| PR | Alter | Draft |")
        add("|----|-------|-------|")
        for p in a["prs"]:
            add(f"| #{p['number']} {p['title']} | {fmt_days(p['age_days'])} "
                f"| {'✓' if p['draft'] else ''} |")
        add("")
    else:
        add("## 🔄 Pull Requests")
        add("")
        add("_Keine offenen PRs._")
        add("")

    bf = a["bus_factor"]
    add("## 👥 Bus-Faktor (Top-Contributors, letzter Commit-Chunk)")
    add("")
    if bf["contributors"]:
        add("| Contributor | Commits | Anteil |")
        add("|---------------|---------|--------|")
        for c in bf["contributors"]:
            share = (c["contributions"] / bf["total_commits_sampled"]
                     * 100 if bf["total_commits_sampled"] else 0)
            add(f"| @{c['login']} | {c['contributions']} | {share:.0f}% |")
        add("")
        add(f"_(Gesamtstichprobe: {bf['total_commits_sampled']} Commits)_")
    else:
        add("_Keine Contributor-Daten._")
    add("")

    act = a["activity"]
    add("## 📈 Aktivität (letzte 8 Wochen)")
    add("")
    add("| Woche (Start) | Commits | Neue Issues |")
    add("|---------------|---------|-------------|")
    for w in act["weeks"]:
        add(f"| {w['week']} | {w['commits']} | {w['issues']} |")
    add("")
    add(f"Letzter Commit: {act['last_commit'] or '—'} · "
        f"Letztes Release: {act['last_release'] or '—'}")
    add("")

    if a["string_queues"]:
        add("## 🗂️ String-Queues (Datei-Scan)")
        add("")
        add("| Datei | Einträge | Schätzung (menschlich) |")
        add("|-------|----------|------------------------|")
        for sf in a["string_queues"]:
            add(f"| `{sf['path']}` | {sf['entries'] or '–'} | "
                f"{str(sf['estimated_days']) + ' Tage' if sf['estimated_days'] else '–'} |")
        add("")

    add("---")
    add("*Bottleneck Radar — AI orchestriert, Menschen entscheiden. "
        "Metriken: GitHub REST API. Nur Metadaten, keine Inhalte.*")
    add("")
    return "\n".join(L)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Bottleneck Radar")
    p.add_argument("--owner", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--token", default=None,
                   help="GitHub token (optional for public repos)")
    p.add_argument("--out", default=None, help="Markdown report path")
    p.add_argument("--json", default=None, dest="json_out",
                   help="JSON output path")
    p.add_argument("--string-file", action="append", default=[],
                   help="JSON file path in the repo to count entries "
                        "(repeatable)")
    p.add_argument("--daily-rate", type=int, default=50,
                   help="human review rate per day (default 50)")
    p.add_argument("--stale-days", type=int, default=14)
    args = p.parse_args()

    import os
    token = args.token or os.environ.get("GITHUB_TOKEN")

    print(f"→ Sammle Daten von {args.owner}/{args.repo} …", file=sys.stderr)
    data = collect(args.owner, args.repo, token, args.string_file)
    print("→ Analysiere …", file=sys.stderr)
    result = {
        "owner": args.owner,
        "repo": args.repo,
        "repo": data["repo"],
        "analysis": analyze(data, args.daily_rate, args.stale_days),
    }

    report = render_report(args.owner, args.repo, result)

    def _ensure_dir(path: str) -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    if args.out:
        _ensure_dir(args.out)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(report)
        print(f"→ Report: {args.out}", file=sys.stderr)
    if args.json_out:
        _ensure_dir(args.json_out)
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print(f"→ JSON: {args.json_out}", file=sys.stderr)
    if not args.out and not args.json_out:
        print(report)

    # console summary
    print("\n=== TOP-BEFUNDE ===", file=sys.stderr)
    for n, f in enumerate(result["analysis"]["findings"][:5], 1):
        print(f"{n}. {SEV_ICON.get(f['severity'], '·')} {f['title']}: "
              f"{f['detail']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
