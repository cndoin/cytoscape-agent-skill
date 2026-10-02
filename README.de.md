# Cytoscape Agent Skill

**Steuern Sie die originale Cytoscape-Desktop-Engine mit KI-Agenten.** Cytoscape führt Analyse und Visualisierung aus; der Agent wählt dokumentierte Befehle. Dieses Projekt implementiert keine Algorithmen neu und ersetzt Cytoscape nicht durch NetworkX oder igraph.

[简体中文](README.zh-CN.md) · [English](README.md) · [Español](README.es.md) · [Français](README.fr.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Português](README.pt-BR.md) · [Русский](README.ru.md) · [العربية](README.ar.md) · [हिन्दी](README.hi.md)

## Funktionen

- Netzwerke, Tabellen, Sitzungen und visuelle Ressourcen über CyREST und Commands API importieren und exportieren.
- Verfügbare Befehle und Parameter vor der Ausführung ermitteln; bei einem Workflow-Fehler wird sofort abgebrochen.
- MCP-Clients über Streamable HTTP oder die enthaltene stdio-Brücke verbinden.
- Konfigurationen für neun Client-Familien und Herkunftsmanifeste mit Engine-Version und SHA-256-Fingerprints erstellen.
- Umgebung prüfen, Engines erkennen und Konfigurationen sicher schreiben.

## Schnellstart

Voraussetzungen: Python 3.8+, Cytoscape Desktop 3.10+ und Java 17+. Es sind keine zusätzlichen Python-Pakete nötig.

```bash
git clone https://github.com/cndoin/cytoscape-agent-skill.git
cd cytoscape-agent-skill/cytoscape-agent-skill
python scripts/cyctl.py doctor
python scripts/cyctl.py discover
python scripts/cyctl.py start --engine system
python scripts/cyctl.py wait
python scripts/cyctl.py commands network
```

Vor der Installation der festgelegten Engine bitte [`INSTALL.md`](INSTALL.md) und `docs/03-跨Agent接入指南.md` lesen. Downloads und Installer verändern das lokale System; Quelle und Integrität vorher prüfen.

## Agent verbinden

```bash
python scripts/cyctl.py mcp --host codex
python scripts/cyctl.py mcp --write-config --only-dedicated --project . --scope project
```

Der erste Befehl zeigt nur die Konfiguration. Der zweite schreibt sie in das Projekt. Gemischte Konfigurationsdateien werden nicht automatisch geändert. Für stdio-Clients wie Claude Desktop: `python scripts/cyctl.py bridge --port 1234`. Details in `docs/03-跨Agent接入指南.md`.

## Sicherheit und Grenzen

- Layouts können zufällig sein; ein Herkunftsmanifest garantiert keine identischen Koordinaten.
- Bei `system` oder `attach` muss die tatsächliche Engine-Version angegeben werden.
- PNG/PDF/SVG/CX mit `rest ... --out DATEI` exportieren, damit Binärdaten unverändert bleiben.
- Nicht überprüfbare Downloads werden standardmäßig blockiert; gefährliche Befehle sind geschützt.

## Prüfung

```bash
python -m unittest discover -s tests -v
python tools/package.py --verify
```

Echte Engine-Tests benötigen Cytoscape samt Apps. `docs/07` und `docs/08` sind datierte Prüfberichte, keine allgemeine Garantie. Siehe [`SKILL.md`](cytoscape-agent-skill/SKILL.md), [`AGENTS.md`](cytoscape-agent-skill/AGENTS.md) und [`INSTALL.md`](INSTALL.md).

Code und Dokumentation stehen unter LGPL-2.1 (`LICENSE`). Cytoscape Desktop und Apps sind separate Upstream-Software und nicht enthalten. Beiträge sind willkommen: [`CONTRIBUTING.md`](CONTRIBUTING.md).
