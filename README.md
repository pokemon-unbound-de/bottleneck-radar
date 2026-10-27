# 📡 Bottleneck Radar

**Data-drivene Flaschenhals-Erkennung für GitHub-Projekte.**

Bottleneck Radar misst ein GitHub-Repository und findet, wo Arbeit feststeckt:
gealter Backlog, PR-Warteschlangen, Bus-Faktor, Release-Lücken, String-Queues.
Das Ergebnis ist ein verständlicher Report mit Top-5-Flaschenhälsen und
konkreten Empfehlungen.

> **Designprinzip: AI orchestriert, Menschen entscheiden.**
> Das Tool misst und schlägt vor — Menschen entscheiden, was als Nächstes
> passiert.

## Was es misst

| Kennzahl | Wofür |
|---|---|
| Backlog & Aging | Wie alt sind offene Issues? (ältestes, Median, veraltet > 14 Tage) |
| Label-Verteilung | In welchen Kategorien staut sich Arbeit? |
| PR-Gesundheit | Wie viele PRs warten? Wie lange? |
| Bus-Faktor | Wie sehr hängt das Projekt an 1–2 Personen? |
| Release-Kadenz | Hängen Commits unveröffentlicht? |
| Aktivität (8 Wochen) | Kommt das Projekt in Fahrt oder nicht? |
| String-Queues (optional) | Wie viele Übersetzungs-/Content-Einträge warten in JSON-Dateien? |

## Installation

Keine Abhängigkeiten — reines Python 3.10+ (nur Standardbibliothek):

```sh
git clone https://github.com/pokemon-unbound-de/bottleneck-radar.git
```

## Nutzung

```sh
# Öffentliches Repo (ohne Token)
python3 bottleneck_radar.py --owner <owner> --repo <name>

# Mit Token (höhere Rate-Limits, private Repos)
python3 bottleneck_radar.py --owner <owner> --repo <name> --token ghp_xxx

# Mit String-Queue-Scan (z. B. Übersetzungs-JSON)
python3 bottleneck_radar.py --owner <owner> --repo <name> \
    --token ghp_xxx \
    --string-file review/cards_stage_extended.json \
    --daily-rate 50

# Ausgaben
python3 bottleneck_radar.py ... --out report.md --json report.json
```

### Optionen

| Option | Bedeutung |
|---|---|
| `--owner` / `--repo` | GitHub-Projekt |
| `--token` | GitHub-PAT (alternativ Umgebungsvariable `GITHUB_TOKEN`) |
| `--string-file` | JSON-Pfad im Repo, dessen Einträge gezählt werden (wiederholbar) |
| `--daily-rate` | Menschlicher Durchsatz pro Tag für Queue-Schätzung (Default 50) |
| `--stale-days` | Ab wie vielen Tagen inaktiv ein Issue "veraltet" ist (Default 14) |
| `--out` / `--json` | Report-Ausgaben |

## Schweregrade

| Symbol | Bedeutung |
|---|---|
| 🔴 | Kritisch — blockiert das Projekt (z. B. Bus-Faktor 1) |
| 🟠 | Warnung — sollte zeitnah angegangen werden |
| 🟢 | Info — beobachten |

## Beispiele

Report-Beispiele landen in [`reports/`](reports/) — nur mit Daten aus
öffentlichen Repos.

## Sicherheit

- **Keine ROMs, Inhalte oder privaten Daten** in Reports.
- Der Token wird nie in Reports, Logs oder Commits abgelegt.
- `--string-file` zählt nur Einträge (Metadaten), liest keine Inhalte.

## Lizenz

[MIT](LICENSE) — Copyright (c) 2026 pokemon-unbound-de

---

*Teil des Pokémon Unbound DE Projekts. Unoffizielles Fan-Projekt, nicht mit
den Rechteinhabern verbunden.*
