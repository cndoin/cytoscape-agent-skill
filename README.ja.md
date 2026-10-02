# Cytoscape Agent Skill

**AI エージェントから Cytoscape Desktop の純正エンジンを操作します。** 解析と可視化は Cytoscape が実行し、エージェントは公式コマンドを選択します。NetworkX/igraph でアルゴリズムを代替実装することはありません。

[简体中文](README.zh-CN.md) · [English](README.md) · [Español](README.es.md) · [Français](README.fr.md) · [Deutsch](README.de.md) · [한국어](README.ko.md) · [Português](README.pt-BR.md) · [Русский](README.ru.md) · [العربية](README.ar.md) · [हिन्दी](README.hi.md)

## 機能

- CyREST と Commands API を使ったネットワーク、テーブル、セッション、画像リソースの入出力。
- 実際のコマンドと引数を調べてから実行し、ワークフローの失敗時は停止。
- Streamable HTTP または付属の stdio ブリッジで MCP 対応クライアントに接続。
- 9 種類のクライアント設定を生成。エンジン版、入力 SHA-256、実行結果を記録するマニフェストを作成。
- 環境診断、エンジン検出、安全な設定書き込み。

## クイックスタート

必要環境: Python 3.8 以降、Cytoscape Desktop 3.10 以降、Java 17 以降。追加の Python パッケージは不要です。

```bash
git clone https://github.com/cndoin/cytoscape-agent-skill.git
cd cytoscape-agent-skill/cytoscape-agent-skill
python scripts/cyctl.py doctor
python scripts/cyctl.py discover
python scripts/cyctl.py start --engine system
python scripts/cyctl.py wait
python scripts/cyctl.py commands network
```

固定版エンジンを導入する前に [`INSTALL.md`](INSTALL.md) と `docs/03-跨Agent接入指南.md` を確認してください。ダウンロードやインストーラーはローカル環境を変更するため、出所と検証状態を確認します。

## エージェント接続

```bash
python scripts/cyctl.py mcp --host codex
python scripts/cyctl.py mcp --write-config --only-dedicated --project . --scope project
```

最初のコマンドは設定の表示のみです。2つ目はプロジェクト設定を書き込みます。複合設定ファイルは自動変更しません。Claude Desktop など stdio 専用クライアントには `python scripts/cyctl.py bridge --port 1234` を使用します。詳細は `docs/03-跨Agent接入指南.md` を参照してください。

## 安全性と制限

- レイアウトには乱数性がある場合があります。実行マニフェストは同一座標を保証しません。
- `system`/`attach` 使用時は実際のエンジンバージョンを記録します。
- PNG/PDF/SVG/CX は `rest ... --out FILE` で出力し、バイナリを保持します。
- 検証できないダウンロードは既定で拒否し、危険なコマンドを保護します。

## 検証

```bash
python -m unittest discover -s tests -v
python tools/package.py --verify
```

実エンジンのテストには Cytoscape と対象アプリが必要です。`docs/07` と `docs/08` は日付付きの検証記録で、将来すべての環境での保証ではありません。[`SKILL.md`](cytoscape-agent-skill/SKILL.md)、[`AGENTS.md`](cytoscape-agent-skill/AGENTS.md)、[`INSTALL.md`](INSTALL.md) を参照してください。

コードと文書は LGPL-2.1（[`LICENSE`](LICENSE)）です。Cytoscape Desktop と各アプリは別個の上流ソフトウェアで、本リポジトリには含みません。貢献歓迎: [`CONTRIBUTING.md`](CONTRIBUTING.md)。
