# Cytoscape Agent Skill

**Pilotez le moteur original de Cytoscape Desktop depuis des agents IA.** Cytoscape réalise toujours les calculs et la visualisation ; l’agent choisit les commandes documentées. Le projet ne réimplémente aucun algorithme et ne remplace pas Cytoscape par NetworkX ou igraph.

[简体中文](README.zh-CN.md) · [English](README.md) · [Español](README.es.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Português](README.pt-BR.md) · [Русский](README.ru.md) · [العربية](README.ar.md) · [हिन्दी](README.hi.md)

## Fonctionnalités

- Import et export de réseaux, tableaux, sessions et ressources visuelles via CyREST et Commands API.
- Découverte des commandes et paramètres réels avant exécution ; tout échec arrête le workflow.
- Connexion MCP en Streamable HTTP ou via le pont stdio fourni.
- Configuration pour neuf familles de clients et manifeste de provenance avec version du moteur et empreintes SHA-256.
- Diagnostic de l’environnement, détection du moteur et écriture sûre des configurations.

## Démarrage rapide

Prérequis : Python 3.8+, Cytoscape Desktop 3.10+ et Java 17+. Aucune dépendance Python tierce n’est requise.

```bash
git clone https://github.com/cndoin/cytoscape-agent-skill.git
cd cytoscape-agent-skill/cytoscape-agent-skill
python scripts/cyctl.py doctor
python scripts/cyctl.py discover
python scripts/cyctl.py start --engine system
python scripts/cyctl.py wait
python scripts/cyctl.py commands network
```

Avant d’installer le moteur verrouillé, consultez [`INSTALL.md`](INSTALL.md) et `docs/03-跨Agent接入指南.md`. Un téléchargement ou un installateur modifie l’environnement local : vérifiez la provenance et l’intégrité.

## Connexion d’un agent

```bash
python scripts/cyctl.py mcp --host codex
python scripts/cyctl.py mcp --write-config --only-dedicated --project . --scope project
```

La première commande affiche seulement la configuration ; la seconde l’écrit dans le projet. Les fichiers de configuration mixtes ne sont pas réécrits automatiquement. Pour un client stdio comme Claude Desktop : `python scripts/cyctl.py bridge --port 1234`. Voir `docs/03-跨Agent接入指南.md`.

## Sécurité et limites

- Certains layouts sont aléatoires ; le manifeste n’assure pas des coordonnées identiques.
- Avec `system` ou `attach`, indiquez la version réelle du moteur.
- Exportez PNG/PDF/SVG/CX avec `rest ... --out FICHIER` afin de préserver les octets.
- Les téléchargements invérifiables sont bloqués par défaut et les commandes dangereuses sont protégées.

## Validation

```bash
python -m unittest discover -s tests -v
python tools/package.py --verify
```

Les tests sur moteur réel nécessitent Cytoscape et ses apps. `docs/07` et `docs/08` sont des rapports datés, pas une garantie universelle. Consultez [`SKILL.md`](cytoscape-agent-skill/SKILL.md), [`AGENTS.md`](cytoscape-agent-skill/AGENTS.md) et [`INSTALL.md`](INSTALL.md).

Le code et la documentation sont sous LGPL-2.1 (`LICENSE`). Cytoscape Desktop et ses apps sont des logiciels amont distincts, non inclus. Contributions bienvenues : [`CONTRIBUTING.md`](CONTRIBUTING.md).
