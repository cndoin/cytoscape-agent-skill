# Cytoscape Agent Skill

**AI 에이전트에서 Cytoscape Desktop의 원본 엔진을 제어합니다.** 분석과 시각화는 Cytoscape가 수행하고 에이전트는 문서화된 명령을 선택합니다. NetworkX/igraph로 알고리즘을 다시 구현하거나 대체하지 않습니다.

[简体中文](README.zh-CN.md) · [English](README.md) · [Español](README.es.md) · [Français](README.fr.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [Português](README.pt-BR.md) · [Русский](README.ru.md) · [العربية](README.ar.md) · [हिन्दी](README.hi.md)

## 기능

- CyREST 및 Commands API로 네트워크, 테이블, 세션, 시각 자료를 가져오고 내보냅니다.
- 실행 전에 실제 명령과 매개변수를 조회하고, 워크플로 한 단계가 실패하면 중단합니다.
- Streamable HTTP 또는 내장 stdio 브리지로 MCP 클라이언트를 연결합니다.
- 9개 클라이언트 유형별 설정과 엔진 버전, 입력 SHA-256, 실행 결과 지문을 기록하는 매니페스트를 만듭니다.
- 환경 진단, 엔진 검색, 안전한 설정 파일 쓰기를 제공합니다.

## 빠른 시작

요구 사항: Python 3.8+, Cytoscape Desktop 3.10+, Java 17+. 추가 Python 패키지는 필요하지 않습니다.

```bash
git clone https://github.com/cndoin/cytoscape-agent-skill.git
cd cytoscape-agent-skill/cytoscape-agent-skill
python scripts/cyctl.py doctor
python scripts/cyctl.py discover
python scripts/cyctl.py start --engine system
python scripts/cyctl.py wait
python scripts/cyctl.py commands network
```

고정 버전 엔진을 설치하기 전에 [`INSTALL.md`](INSTALL.md)와 `docs/03-跨Agent接入指南.md`를 읽으세요. 다운로드와 설치 프로그램 실행은 로컬 환경을 변경하므로 출처와 무결성을 검토해야 합니다.

## 에이전트 연결

```bash
python scripts/cyctl.py mcp --host codex
python scripts/cyctl.py mcp --write-config --only-dedicated --project . --scope project
```

첫 명령은 설정을 미리보기만 합니다. 두 번째 명령은 프로젝트 설정 파일에 기록합니다. 여러 용도의 설정 파일은 자동 수정하지 않습니다. Claude Desktop 등 stdio 전용 클라이언트는 `python scripts/cyctl.py bridge --port 1234`를 사용하세요. 자세한 내용은 `docs/03-跨Agent接入指南.md`를 참고하세요.

## 안전 및 한계

- 레이아웃에는 무작위성이 있을 수 있으므로 매니페스트가 동일 좌표를 보장하지 않습니다.
- `system` 또는 `attach` 모드에서는 실제 엔진 버전을 보고해야 합니다.
- PNG/PDF/SVG/CX는 바이트 보존을 위해 `rest ... --out FILE`로 내보냅니다.
- 검증할 수 없는 다운로드는 기본적으로 차단하고 위험 명령은 보호합니다.

## 검증

```bash
python -m unittest discover -s tests -v
python tools/package.py --verify
```

실제 엔진 테스트에는 Cytoscape와 해당 앱이 필요합니다. `docs/07`, `docs/08`은 날짜가 있는 테스트 증거이며 모든 환경에 대한 영구 보증은 아닙니다. [`SKILL.md`](cytoscape-agent-skill/SKILL.md), [`AGENTS.md`](cytoscape-agent-skill/AGENTS.md), [`INSTALL.md`](INSTALL.md)를 확인하세요.

코드와 문서는 LGPL-2.1([`LICENSE`](LICENSE))입니다. Cytoscape Desktop 및 앱은 별도의 상위 소프트웨어이며 저장소에 포함되지 않습니다. 기여를 환영합니다: [`CONTRIBUTING.md`](CONTRIBUTING.md).
