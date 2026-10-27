#!/usr/bin/env python3
"""
Bottleneck Radar
================
Data-driven bottleneck detection for GitHub projects.

Analyzes a repository for:
  * aged issue backlog & staleness (with aging histogram)
  * label distribution (where work piles up)
  * pull-request queue health & merge cycle time
  * contributor concentration / bus factor (commit authors + linked logins)
  * release cadence vs. development activity
  * translation/asset string queues (optional file scan:
    entry counts, queue aging, category/unit distributions)

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

v0.2:
  * queue distribution scan (low-cardinality fields: category/unit/status)
  * queue aging via date fields
  * bus factor from commit authors (works without linked accounts)
  * PR merge cycle time (closed & merged PRs)
  * issue aging histogram
  * download_url fallback for files > 1 MB
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import statistics
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.github.com"
UA = "bottleneck-radar"
DATE_FIELDS = ("created_at", "created", "date", "timestamp",
               "updated_at", "added_at", "ts", "time")


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


def parse_ts(value) -> datetime | None:
    if not isinstance(value, str):
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
    if d < 1:
        return f"{d * 24:.0f} h"
    return f"{d:.0f} T" if d < 60 else f"{d / 30.0:.1f} M"


AI_AUTHOR_HINTS = (
    "claude", "anthropic", "codex", "openai", "copilot", "zenflow",
    "cursor", "aider", "cline", "windsurf", "gemini", "llm", "ai-agent",
    "[bot]", "+bot", "noreply",
)


def is_ai_author(name: str | None, email: str | None) -> bool:
    """Heuristic: is this commit author an AI agent/bot?"""
    hay = f"{name or ''} {email or ''}".lower()
    return any(h in hay for h in AI_AUTHOR_HINTS)


def fetch_file_json(url: str, token: str | None):
    """Fetch a JSON file from the repo (contents API + raw fallback)."""
    try:
        data = api(url, token)
    except (urllib.error.HTTPError, urllib.error.URLError):
        return None
    if isinstance(data, dict):
        raw = data.get("content")
        if raw:
            try:
                return json.loads(base64.b64decode(raw))
            except Exception:
                pass
        dl = data.get("download_url")
        if dl:
            headers = {"User-Agent": UA}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            try:
                req = urllib.request.Request(dl, headers=headers)
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read().decode())
            except Exception:
                return None
    return None


# --------------------------------------------------------------------------
# Data collection
# --------------------------------------------------------------------------

def collect(owner: str, repo: str, token: str | None,
            string_files: list[str]) -> dict:
    out: dict = {}

    out["repo"] = api(f"{API}/repos/{owner}/{repo}", token)
    default_branch = out["repo"].get("default_branch", "main")

    items = api_paginated(
        f"{API}/repos/{owner}/{repo}/issues?state=open", token)
    out["issues"] = [i for i in items if "pull_request" not in i]
    out["pulls"] = [i for i in items if "pull_request" in i]

    out["closed_pulls"] = api_paginated(
        f"{API}/repos/{owner}/{repo}/pulls?state=closed", token,
        max_pages=5)

    out["contributors"] = api(
        f"{API}/repos/{owner}/{repo}/contributors?per_page=100", token)
    out["releases"] = api(
        f"{API}/repos/{owner}/{repo}/releases?per_page=10", token)
    out["commits"] = api(
        f"{API}/repos/{owner}/{repo}/commits?per_page=100", token)

    # queue files: parsed in memory only (never written to output)
    queues: list[dict] = []
    for path in string_files:
        print(f"→ Lade Queue-Datei: {path} …", file=sys.stderr)
        parsed = fetch_file_json(
            f"{API}/repos/{owner}/{repo}/contents/{path}?ref="
            f"{default_branch}", token)
        queues.append({"path": path, "parsed": parsed})
    out["queues"] = queues

    return out


def largest_collection(parsed):
    """Extract the list (or largest list/dict) from a parsed queue file."""
    if parsed is None:
        return None
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        sizes = [(len(v), k) for k, v in parsed.items()
                 if isinstance(v, (list, dict))]
        if sizes:
            biggest = max(sizes)[1]
            return parsed[biggest]
    return None


# --------------------------------------------------------------------------
# Queue analysis
# --------------------------------------------------------------------------

def queue_stats(parsed, now: datetime) -> dict:
    entries = largest_collection(parsed)
    if entries is None:
        return {"entries": None}
    if isinstance(entries, dict):
        entries = list(entries.values())
    if not isinstance(entries, list):
        return {"entries": None}

    stat: dict = {"entries": len(entries)}
    if not entries or not isinstance(entries[0], dict):
        return stat

    # --- queue aging via date fields
    sample_n = min(len(entries), 200)
    for f in DATE_FIELDS:
        vals = [e.get(f) for e in entries[:sample_n]
                if isinstance(e.get(f), str)]
        if len(vals) >= max(1, sample_n // 2):
            parsed_ts = [t for t in (parse_ts(v) for v in vals) if t]
            if parsed_ts:
                ages = sorted(age_days(t, now) or 0 for t in parsed_ts)
                stat["aging"] = {
                    "date_field": f,
                    "oldest_days": round(max(ages), 1),
                    "median_days": round(statistics.median(ages), 1),
                }
                break

    # --- distributions over low-cardinality string fields
    counts: dict[str, dict[str, int]] = {}
    for e in entries[:5000]:
        for k, v in e.items():
            if isinstance(v, str) and v and len(v) <= 60:
                counts.setdefault(k, {})
                counts[k][v] = counts[k].get(v, 0) + 1
    dists = []
    for k, vals in counts.items():
        distinct = len(vals)
        if 2 <= distinct <= 30:
            top = sorted(vals.items(), key=lambda kv: -kv[1])[:8]
            dists.append({
                "field": k,
                "distinct": distinct,
                "top": [(t[:50], c) for t, c in top],
                "top1_share": round(top[0][1] / min(len(entries), 5000)
                                    * 100, 1),
            })
    dists.sort(key=lambda d: d["distinct"])
    if dists:
        stat["distributions"] = dists[:4]
    return stat


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
             if (a := age_days(parse_ts(i.get("updated_at")), now))
             and a >= stale_days]

    buckets = {"0-7 T": 0, "8-14 T": 0, "15-30 T": 0, ">30 T": 0}
    for a in ages:
        if a <= 7:
            buckets["0-7 T"] += 1
        elif a <= 14:
            buckets["8-14 T"] += 1
        elif a <= 30:
            buckets["15-30 T"] += 1
        else:
            buckets[">30 T"] += 1

    # --- labels
    label_counts: dict[str, int] = {}
    for i in issues:
        for lab in i.get("labels", []):
            name = lab.get("name", "") if isinstance(lab, dict) else str(lab)
            label_counts[name or "(unlabeled)"] = (
                label_counts.get(name or "(unlabeled)", 0) + 1)
    labels = sorted(label_counts.items(), key=lambda kv: -kv[1])

    # --- PRs (open)
    pr_info = []
    for p in pulls:
        pr_info.append({
            "number": p["number"],
            "title": p["title"][:80],
            "age_days": age_days(parse_ts(p["created_at"]), now),
            "draft": bool(p.get("pull_request", {}).get("draft")),
        })

    # --- PR merge cycle (closed & merged)
    merged_deltas = []
    for p in data.get("closed_pulls", []):
        c, m = parse_ts(p.get("created_at")), parse_ts(p.get("merged_at"))
        if c and m:
            merged_deltas.append((m - c).total_seconds() / 86400.0)
    avg_cycle = statistics.mean(merged_deltas) if merged_deltas else None

    # --- bus factor: commit authors, split human vs AI
    author_counts: dict[str, int] = {}
    ai_commit_count = 0
    for c in commits:
        a = c.get("commit", {}).get("author", {})
        name, email = a.get("name"), a.get("email")
        key = name or email or "?"
        author_counts[key] = author_counts.get(key, 0) + 1
        if is_ai_author(name, email):
            ai_commit_count += 1
    total_commits = sum(author_counts.values())
    top_authors = sorted(author_counts.items(), key=lambda kv: -kv[1])
    ai_share = (ai_commit_count / total_commits * 100
                if total_commits else None)
    # authors were aggregated by name/email key — re-classify by key
    human_counts = {k: v for k, v in author_counts.items()
                    if not is_ai_author(k, None)}
    total_human = sum(human_counts.values())
    top_humans = sorted(human_counts.items(), key=lambda kv: -kv[1])
    top_human = top_humans[0] if top_humans else (None, 0)
    top_human_share = (top_human[1] / total_human * 100
                       if top_human[0] and total_human else None)

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
    for q in data.get("queues", []):
        qstat = queue_stats(q.get("parsed"), now)
        entries = qstat.get("entries")
        strings.append({
            "path": q["path"],
            "entries": entries,
            "aging": qstat.get("aging"),
            "distributions": qstat.get("distributions"),
            "estimated_days": round(entries / daily_rate)
            if entries else None,
        })

    # --- findings (severity: 3 = critical, 2 = warning, 1 = info)
    findings: list[dict] = []

    def add(sev, title, detail, rec):
        findings.append({"severity": sev, "title": title,
                         "detail": detail, "recommendation": rec})

    if top_human_share is not None and top_human_share >= 99:
        add(3, "Menschen-Bus-Faktor 1",
            f"Alle {total_human} menschlichen Commits stammen von "
            f"{top_human[0]} (AI-Anteil: "
            f"{(f'{ai_share:.0f}%' if ai_share is not None else '–')}).",
            "Die AI übernimmt Execution, aber menschliches Wissen und "
            "Entscheidungen hängen an einer Person. Zweite Person mit "
            "Write-Zugriff einbinden, Handoffs/Entscheidungen "
            "dokumentieren, AI-Workflows als Repo-Assets ablegen.")
    elif top_human_share is not None and top_human_share >= 70:
        add(2, "Menschen-Bus-Faktor niedrig",
            f"{top_human[0]} hat {top_human_share:.0f}% der menschlichen "
            f"Commits ({top_human[1]}/{total_human}).",
            "Code-Reviews verteilen; Pair-Programmierung/Handoffs "
            "dokumentieren, um Wissen zu teilen.")
    if ai_share is not None and ai_share >= 40:
        ai_names = ", ".join(f"{n} ({c})" for n, c in top_authors
                             if is_ai_author(n, None))
        add(1, "Starke AI-Orchestrierung",
            f"{ai_share:.0f}% der Commits stammen von AI-Agenten"
            f" ({ai_names}).",
            "Gute Basis: AI-Workflows (Prompts, Tools, Checks) als "
            "Repo-Assets systematisieren, damit die Orchestrierung "
            "wechselbar bleibt.")

    if ages and oldest >= 21:
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

    dc = age_days(last_commit, now) if last_commit else None
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

    old_prs = [p for p in pr_info if (p["age_days"] or 0) >= stale_days]
    if old_prs:
        add(2, "PR-Warteschlange",
            "Offene PRs: " + ", ".join(
                f"#{p['number']} ({fmt_days(p['age_days'])})"
                for p in old_prs[:6]),
            "PRs im Triage-Kanal besprechen; Review-Fenster oder "
            "Auto-Review durch AI-Codereview einführen.")

    if avg_cycle is not None and avg_cycle > 7:
        add(2, "PR-Zyklus lang",
            f"Ø {avg_cycle:.1f} Tage von Öffnen bis Merge "
            f"({len(merged_deltas)} gemergte PRs).",
            "Review-Fenster festlegen (z. B. 48h); AI-Pre-Reviews "
            "für Checks, die sich wiederholen, automatisieren.")

    for sf in strings:
        if sf.get("entries") and sf["entries"] >= 500:
            add(2, f"String-Queue: {sf['entries']:,} Einträge",
                f"{sf['path']} enthält {sf['entries']:,} Einträge; bei "
                f"{daily_rate}/Tag ≈ {sf['estimated_days']} Tage "
                f"menschlicher Durchlauf.",
                "Durchsatz erhöhen: AI-Vorfilterung (Terminologie, "
                "Textbox-Fit), Fuzzy-Matching gegen Bestand, dann nur "
                "Diffs von Menschen prüfen.")
            ag = sf.get("aging")
            if ag and (ag.get("oldest_days") or 0) >= 14:
                add(2, "Queue veraltet",
                    f"Ältester Eintrag der Queue {sf['path']} ist "
                    f"{fmt_days(ag['oldest_days'])} alt (Median "
                    f"{fmt_days(ag['median_days'])}).",
                    "Älteste Einträge zuerst prüfen (Frische-Wert) oder "
                    "Queue priorisieren, bevor sich Wissen verfällt.")
            for d in (sf.get("distributions") or [])[:1]:
                if d.get("top1_share", 0) >= 60 and d["top"]:
                    add(1, "Queue konzentriert",
                        f"{d['top1_share']:.0f}% der Queue liegt auf "
                        f"`{d['field']} = {d['top'][0][0]}` "
                        f"({d['top'][0][1]}/{sf['entries']}).",
                        "Konzentrierte Queuen lassen sich mit "
                        "batch-fähigen AI-Pre-Checks am effektivsten "
                        "durchzogen (ein Kontext, viele Einträge).")

    if not findings:
        add(1, "Keine kritischen Flaschenhälse erkannt",
            "Alle gemessenen Kennzahlen sind im grünen Bereich.",
            "Kennzahlen regelmäßig (z. B. wöchentlich) neu messen, "
            "um Drift früh zu erkennen.")

    findings.sort(key=lambda f: -f["severity"])

    return {
        "version": "0.3",
        "generated_at": now.isoformat(),
        "backlog": {
            "open": len(issues),
            "oldest_days": round(oldest, 1),
            "median_days": round(median, 1),
            "buckets": buckets,
            "stale": [{"number": i["number"], "title": i["title"][:80]}
                      for i in stale],
        },
        "labels": labels,
        "prs": pr_info,
        "pr_cycle": {
            "merged_count": len(merged_deltas),
            "avg_days": round(avg_cycle, 1) if avg_cycle is not None
            else None,
        },
        "bus_factor": {
            "authors": [{"name": n, "commits": c,
                         "ai": is_ai_author(n, None)}
                        for n, c in top_authors[:10]],
            "linked_contributors": [
                {"login": c["login"], "contributions": c["contributions"]}
                for c in contributors[:10]],
            "total_commits_sampled": total_commits,
            "ai_share_pct": round(ai_share, 1) if ai_share is not None
            else None,
            "total_human_commits": total_human,
            "top_human": {
                "name": top_human[0],
                "commits": top_human[1],
                "share_pct": round(top_human_share, 1)
                if top_human_share is not None else None,
            },
        },
        "activity": {
            "last_commit": str(last_commit),
            "last_release": str(last_release),
            "weeks": weeks,
        },
        "string_queues": [
            {k: v for k, v in sf.items() if k != "distributions"}
            | ({"distributions": sf["distributions"]}
               if sf.get("distributions") else {})
            for sf in strings
        ],
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
        f"`{repo_obj.get('default_branch')}` · Radar v{a.get('version', '0.1')}")
    add("")
    add("## 🎯 Top-Flaschenhälse (nach Schweregrad)")
    add("")
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
    b = a["backlog"]["buckets"]
    add(f"- Verteilung: 0–7 T: {b['0-7 T']} · 8–14 T: {b['8-14 T']} · "
        f"15–30 T: {b['15-30 T']} · >30 T: {b['>30 T']}")
    if a["backlog"]["stale"]:
        add(f"- Veraltet (≥ 14 Tage inaktiv): "
            + ", ".join(f"#{s['number']}" for s in a["backlog"]["stale"]))
    add("")
    if a["labels"]:
        add("**Verteilung nach Labels:** "
            + " · ".join(f"`{k}` ({v})" for k, v in a["labels"]))
        add("")

    add("## 🔄 Pull Requests")
    add("")
    if a["prs"]:
        add("| PR | Alter | Draft |")
        add("|----|-------|-------|")
        for p in a["prs"]:
            add(f"| #{p['number']} {p['title']} | {fmt_days(p['age_days'])} "
                f"| {'✓' if p['draft'] else ''} |")
        add("")
    else:
        add("_Keine offenen PRs._")
        add("")
    pc = a["pr_cycle"]
    if pc["merged_count"]:
        add(f"**Merge-Zyklus:** Ø {pc['avg_days']} Tage "
            f"({pc['merged_count']} gemergte PRs, letztes Fenster).")
        add("")

    bf = a["bus_factor"]
    add("## 👥 Bus-Faktor (Commits, letzter Stichproben-Chunk)")
    add("")
    if bf["authors"]:
        add("| Author | Commits | Anteil | Typ |")
        add("|--------|---------|--------|-----|")
        for c in bf["authors"]:
            share = (c["commits"] / bf["total_commits_sampled"] * 100
                     if bf["total_commits_sampled"] else 0)
            typ = "🤖 AI" if c.get("ai") else "👤 Mensch"
            add(f"| {c['name']} | {c['commits']} | {share:.0f}% | {typ} |")
        add("")
        if bf.get("ai_share_pct") is not None:
            add(f"**AI-Anteil:** {bf['ai_share_pct']:.0f}% · "
                f"**Menschen-Bus-Faktor:** {bf['top_human']['name']} "
                f"({bf['top_human']['share_pct']:.0f}% der "
                f"{bf['total_human_commits']} menschlichen Commits)")
            add("")
        linked = ""
        if bf["linked_contributors"]:
            logins = ", ".join("@" + c["login"]
                               for c in bf["linked_contributors"][:4])
            linked = f" · verlinkte Accounts: {logins}"
        add(f"_(Stichprobe: {bf['total_commits_sampled']} Commits{linked})_")
    else:
        add("_Keine Commit-Daten._")
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
        add("| Datei | Einträge | Schätzung (menschlich) | Ältester Eintrag |")
        add("|-------|----------|------------------------|------------------|")
        for sf in a["string_queues"]:
            ag = sf.get("aging")
            add(f"| `{sf['path']}` | {sf['entries'] or '–'} | "
                f"{str(sf['estimated_days']) + ' Tage' if sf['estimated_days'] else '–'} | "
                f"{fmt_days(ag['oldest_days']) if ag else '–'} |")
        add("")
        for sf in a["string_queues"]:
            for d in (sf.get("distributions") or []):
                top = " · ".join(f"`{t}` ({c})" for t, c in d["top"][:5])
                add(f"**Verteilung** `{sf['path']}` → "
                    f" Feld `{d['field']}` "
                    f"({d['distinct']} Werte): {top}")
                add("")

    add("---")
    add("*Bottleneck Radar — AI orchestriert, Menschen entscheiden. "
        "Metriken: GitHub REST API + Datei-Metadaten, keine Inhalte.*")
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
                   help="JSON file path in the repo to scan (repeatable)")
    p.add_argument("--daily-rate", type=int, default=50,
                   help="human review rate per day (default 50)")
    p.add_argument("--stale-days", type=int, default=14)
    args = p.parse_args()

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

    print("\n=== TOP-BEFUNDE ===", file=sys.stderr)
    for n, f in enumerate(result["analysis"]["findings"][:6], 1):
        print(f"{n}. {SEV_ICON.get(f['severity'], '·')} {f['title']}: "
              f"{f['detail']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
