# Cytoscape Agent Skill

**Controle o mecanismo original do Cytoscape Desktop por agentes de IA.** O Cytoscape continua executando as análises e visualizações; o agente escolhe comandos documentados. O projeto não reimplementa algoritmos nem substitui o Cytoscape por NetworkX ou igraph.

[简体中文](README.zh-CN.md) · [English](README.md) · [Español](README.es.md) · [Français](README.fr.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Русский](README.ru.md) · [العربية](README.ar.md) · [हिन्दी](README.hi.md)

## Recursos

- Importe e exporte redes, tabelas, sessões e recursos visuais pela CyREST e Commands API.
- Consulte os comandos e parâmetros reais antes de executar; uma etapa com falha interrompe o fluxo.
- Conecte clientes MCP por Streamable HTTP ou pela ponte stdio incluída.
- Gere configurações para nove famílias de clientes e manifestos de proveniência com versão do mecanismo e hashes SHA-256.
- Diagnostique o ambiente, localize o mecanismo e grave configurações com segurança.

## Início rápido

Requisitos: Python 3.8+, Cytoscape Desktop 3.10+ e Java 17+. Não há dependências Python de terceiros.

```bash
git clone https://github.com/cndoin/cytoscape-agent-skill.git
cd cytoscape-agent-skill/cytoscape-agent-skill
python scripts/cyctl.py doctor
python scripts/cyctl.py discover
python scripts/cyctl.py start --engine system
python scripts/cyctl.py wait
python scripts/cyctl.py commands network
```

Antes de instalar o mecanismo fixado, leia [`INSTALL.md`](INSTALL.md) e `docs/03-跨Agent接入指南.md`. Downloads e instaladores alteram o ambiente local; confira a origem e a integridade.

## Conectar um agente

```bash
python scripts/cyctl.py mcp --host codex
python scripts/cyctl.py mcp --write-config --only-dedicated --project . --scope project
```

O primeiro comando apenas mostra a configuração; o segundo grava no projeto. Arquivos de configuração mistos não são alterados automaticamente. Para clientes somente stdio, como Claude Desktop, use `python scripts/cyctl.py bridge --port 1234`. Consulte `docs/03-跨Agent接入指南.md`.

## Segurança e limites

- Alguns layouts são aleatórios; o manifesto de proveniência não garante coordenadas idênticas.
- Nos modos `system` e `attach`, informe a versão real do mecanismo.
- Exporte PNG/PDF/SVG/CX com `rest ... --out ARQUIVO` para preservar os bytes.
- Downloads não verificáveis são bloqueados por padrão e comandos perigosos são protegidos.

## Validação

```bash
python -m unittest discover -s tests -v
python tools/package.py --verify
```

Testes com mecanismo real exigem Cytoscape e seus apps. `docs/07` e `docs/08` são evidências datadas, não garantias permanentes. Consulte [`SKILL.md`](cytoscape-agent-skill/SKILL.md), [`AGENTS.md`](cytoscape-agent-skill/AGENTS.md) e [`INSTALL.md`](INSTALL.md).

Código e documentação sob LGPL-2.1 ([`LICENSE`](LICENSE)). Cytoscape Desktop e seus apps são softwares upstream separados e não estão incluídos. Contribuições são bem-vindas: [`CONTRIBUTING.md`](CONTRIBUTING.md).
