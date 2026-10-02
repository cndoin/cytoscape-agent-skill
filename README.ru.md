# Cytoscape Agent Skill

**Управляйте оригинальным движком Cytoscape Desktop с помощью ИИ-агентов.** Анализ и визуализацию выполняет Cytoscape, а агент выбирает документированные команды. Проект не переписывает алгоритмы и не подменяет Cytoscape библиотеками NetworkX или igraph.

[简体中文](README.zh-CN.md) · [English](README.md) · [Español](README.es.md) · [Français](README.fr.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Português](README.pt-BR.md) · [العربية](README.ar.md) · [हिन्दी](README.hi.md)

## Возможности

- Импорт и экспорт сетей, таблиц, сессий и визуальных ресурсов через CyREST и Commands API.
- Поиск реальных команд и параметров перед запуском; при ошибке шагов workflow выполнение прекращается.
- Подключение MCP-клиентов через Streamable HTTP или встроенный мост stdio.
- Конфигурации для девяти семейств клиентов и манифесты происхождения с версией движка и SHA-256.
- Проверка среды, поиск движка и безопасная запись конфигурации.

## Быстрый старт

Требования: Python 3.8+, Cytoscape Desktop 3.10+ и Java 17+. Дополнительные Python-пакеты не нужны.

```bash
git clone https://github.com/cndoin/cytoscape-agent-skill.git
cd cytoscape-agent-skill/cytoscape-agent-skill
python scripts/cyctl.py doctor
python scripts/cyctl.py discover
python scripts/cyctl.py start --engine system
python scripts/cyctl.py wait
python scripts/cyctl.py commands network
```

Перед установкой закреплённого движка прочитайте [`INSTALL.md`](INSTALL.md) и `docs/03-跨Agent接入指南.md`. Загрузка и запуск установщика меняют локальную среду — сначала проверьте источник и целостность.

## Подключение агента

```bash
python scripts/cyctl.py mcp --host codex
python scripts/cyctl.py mcp --write-config --only-dedicated --project . --scope project
```

Первая команда только показывает конфигурацию, вторая записывает её в проект. Смешанные конфигурационные файлы автоматически не изменяются. Для клиентов только со stdio, например Claude Desktop, используйте `python scripts/cyctl.py bridge --port 1234`. Подробности — в `docs/03-跨Agent接入指南.md`.

## Безопасность и ограничения

- Некоторые раскладки случайны; манифест не гарантирует одинаковые координаты.
- В режимах `system` и `attach` указывайте фактическую версию движка.
- Экспортируйте PNG/PDF/SVG/CX через `rest ... --out FILE`, чтобы сохранить байты.
- Непроверяемые загрузки по умолчанию блокируются; опасные команды защищены.

## Проверка

```bash
python -m unittest discover -s tests -v
python tools/package.py --verify
```

Для тестов с реальным движком нужны Cytoscape и соответствующие приложения. `docs/07` и `docs/08` — датированные отчёты, а не постоянная гарантия. См. [`SKILL.md`](cytoscape-agent-skill/SKILL.md), [`AGENTS.md`](cytoscape-agent-skill/AGENTS.md) и [`INSTALL.md`](INSTALL.md).

Код и документация распространяются по LGPL-2.1 ([`LICENSE`](LICENSE)). Cytoscape Desktop и приложения — отдельное ПО upstream и сюда не входят. Вклады приветствуются: [`CONTRIBUTING.md`](CONTRIBUTING.md).
