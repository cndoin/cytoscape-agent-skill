# Cytoscape Agent Skill

**Control el motor original de Cytoscape Desktop desde agentes de IA.** Los cálculos y la visualización siguen ejecutándose en Cytoscape; el agente selecciona comandos documentados. Este proyecto no reimplementa algoritmos ni sustituye Cytoscape por NetworkX o igraph.

[简体中文](README.zh-CN.md) · [English](README.md) · [Français](README.fr.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Português](README.pt-BR.md) · [Русский](README.ru.md) · [العربية](README.ar.md) · [हिन्दी](README.hi.md)

## Funciones

- Importa y exporta redes, tablas, sesiones y recursos visuales mediante CyREST y Commands API.
- Consulta los comandos y parámetros disponibles antes de ejecutarlos; un paso fallido detiene el flujo.
- Conecta clientes compatibles con MCP mediante Streamable HTTP o el puente stdio incluido.
- Genera configuraciones para nueve familias de clientes y manifiestos de procedencia con versión del motor y hashes SHA-256.
- Incluye diagnóstico del entorno, detección del motor y escritura segura de configuración.

## Inicio rápido

Requisitos: Python 3.8+, Cytoscape Desktop 3.10+ y Java 17+. El CLI no necesita dependencias Python de terceros.

```bash
git clone https://github.com/cndoin/cytoscape-agent-skill.git
cd cytoscape-agent-skill/cytoscape-agent-skill
python scripts/cyctl.py doctor
python scripts/cyctl.py discover
python scripts/cyctl.py start --engine system
python scripts/cyctl.py wait
python scripts/cyctl.py commands network
```

Para instalar el motor fijado, lee antes [`INSTALL.md`](INSTALL.md) y `docs/03-跨Agent接入指南.md`. Las descargas y los instaladores cambian el entorno local y requieren revisar su origen e integridad.

## Conectar un agente

```bash
python scripts/cyctl.py mcp --host codex
python scripts/cyctl.py mcp --write-config --only-dedicated --project . --scope project
```

El primer comando solo muestra la configuración. El segundo la escribe en el proyecto. Se rechaza la modificación automática de archivos de configuración mixtos. Para clientes solo stdio, como Claude Desktop, usa `python scripts/cyctl.py bridge --port 1234`. Detalles en `docs/03-跨Agent接入指南.md`.

## Seguridad y límites

- Algunos diseños de red son aleatorios; un manifiesto de procedencia no garantiza coordenadas idénticas.
- Con los modos `system` o `attach`, registra la versión real del motor.
- Exporta PNG/PDF/SVG/CX con `rest ... --out ARCHIVO` para conservar los bytes.
- Las descargas no verificables se bloquean por defecto; los comandos peligrosos están protegidos.

## Validación

```bash
python -m unittest discover -s tests -v
python tools/package.py --verify
```

Las pruebas con motor real requieren Cytoscape y sus apps. Los informes `docs/07` y `docs/08` son evidencias fechadas, no garantías universales. Consulta [`SKILL.md`](cytoscape-agent-skill/SKILL.md), [`AGENTS.md`](cytoscape-agent-skill/AGENTS.md) y [`INSTALL.md`](INSTALL.md).

La skill se publica bajo LGPL-2.1 (`LICENSE`). Cytoscape Desktop y sus apps son software upstream independiente y no se incluyen. Se aceptan contribuciones: [`CONTRIBUTING.md`](CONTRIBUTING.md).
