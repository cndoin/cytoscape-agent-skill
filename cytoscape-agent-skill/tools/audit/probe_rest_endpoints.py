#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""REST 面逐端点真实请求探测 —— 覆盖度审计的取证主体。

设计要点（每一条都对应一个真实踩过的坑）：

  1. **走真实 CLI 入口** `cyctl rest`，而不是直接调 cyrest 库。
     库能通不代表用户实际用的入口能通 —— 参数解析、accept 透传、输出契约
     全在 CLI 层，只测库等于没测。
  2. **逐条断言输出契约**：退出码必须 ∈ {0,2,3,4,5}，stdout 必须是**一个** JSON 对象。
  3. **绝不写死 ID**：网络 / 视图 / 节点 / 边 / 主键 / 样式 / 布局 / 面板名全部现场取。
     写死 ID 会让探测在另一次会话里全部 404，得出完全错误的结论。
  4. **`/v1` 前缀要剥掉**：`cyctl rest` 自己会拼 `/v1`；App 端点（`/cyndex2/v1` 等）
     挂在端口根上，要改用 `--base-url http://host:port`。
  5. **二进制端点走 `--out` 字节通道**，并校验落盘产物的魔数 ——
     只报 `ok:true` 不算数（见 docs/08 §6.1：成功信封里装垃圾）。
  6. **失败要分类，不能一锅端**。「未通过」里混着四种完全不同的东西：
        - 我的探测参数填错了（可修，且修完应通过）
        - 资源本来就不存在，引擎按语义回 404（**证明引擎语义正确**）
        - 上游对不适用的输入回 500（上游缺陷，见 docs/08 §7.4）
        - 真的工具缺陷
     前两类必须显式登记（`EXPECT_ABSENT` / `UPSTREAM_500`），否则报告就是把
     自己的参数错误算到工具头上 —— 本脚本第一版正是这么错的。

前置：
    引擎在跑，且 `evidence/engine-swagger-3.10.5.json` 已存在
    （用 `cyctl rest GET swagger.json --out evidence/engine-swagger-3.10.5.json` 取）。

用法：
    python tools/audit/probe_rest_endpoints.py

退出码：0 全部通过 / 3 缺前置 / 4 有端点未通过（明细见打印表格与 JSON）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent      # skill 根目录
CLI = ROOT / "scripts" / "cyctl.py"
PY = sys.executable
DEFAULT_PORT_ROOT = "http://localhost:1234"
SWAGGER = ROOT / "evidence" / "engine-swagger-3.10.5.json"
OUT = ROOT / "evidence" / "rest-endpoint-probe.json"
#: 二进制产物落盘目录（临时，跑完删）
ARTS = Path(tempfile.mkdtemp(prefix="cyprobe-"))

#: 会改变引擎状态的端点 —— 探了要标注，避免把「探测」当成「只读」。
MUTATING = {
    "/v1/apply/layouts/{algorithmName}/{networkId}",
    "/v1/apply/edgebundling/{networkId}",
    "/v1/apply/clearalledgebends/{networkId}",
    "/v1/apply/styles/{styleName}/{networkId}",
    "/v1/networks/{networkId}/groups/{groupNodeId}/collapse",
    "/v1/networks/{networkId}/groups/{groupNodeId}/expand",
    "/v1/gc",
}
#: 明确声明不探的端点及理由（诚实登记，不是偷偷跳过）。
SKIP = {
    "/v1/session": "GET 需要 file 查询参数（读磁盘上已有的 session 文件）",
    "/aMatReader/v1/predictParameters": "App 专有，需真实 .amat 文件路径",
}
#: 只接受 text/plain 的端点 —— 发 json 会 406。
#: 包含 `commands/{ns}/{cmd}`：它同样是 help 端点（第一版漏了这条，
#: 于是把一条「我们没声明 Accept」错算成「端点坏了」）。
ACCEPT = {
    "/v1/commands": "text/plain",
    "/v1/commands/{namespace}": "text/plain",
    "/v1/commands/{namespace}/{command}": "text/plain",
    "/v1/networks/{networkId}/tables/{tableType}.csv": "text/plain",
    "/v1/networks/{networkId}/tables/{tableType}.tsv": "text/plain",
}

#: 二进制产物端点 —— 必须走 `cyctl rest --out` 的字节通道。
#: 用普通 GET 取它们会 406（引擎按 Accept 协商拒绝），
#: 而一旦改成 decode 成字符串就会静默损坏（见 docs/08 §6.1）。
BINARY_FORMAT = {".png", ".pdf", ".svg", ".cx"}
#: 产物魔数（每个格式可接受多种前缀）。用来证明「拿到的确实是那个格式」。
#: `.cx` 有两种形态：CX2 是 zip（PK），而 **CyREST 实际返回的是 CX JSON**
#: （`Content-Type: application/json`，body 形如 `[{"numberVerification":…`）——
#: 实测 networks/10397.cx 是 119,641 字节的 CX aspect 流，不是 zip。
MAGIC = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".pdf": (b"%PDF",),
    ".svg": (b"<?xml",),
    ".cx": (b"PK\x03\x04", b'[{"', b"[{"),
}

#: 在「刚导入官方示例网络、未做任何分组/映射/嵌套」这一前提下，
#: 以下端点会**按语义**返回 404 —— 资源本来就不存在。
#: 登记在此是为了把它们从「未通过」里摘出来：它们证明**引擎语义正确**，
#: 而不是工具坏了。每条都附现场验证依据。
EXPECT_ABSENT = {
    "/v1/networks/{networkId}/groups/{groupNodeId}":
        "该网络没有分组（`networks/{id}/groups/count` → {\"count\":0}）",
    "/v1/networks/{networkId}/groups/{groupNodeId}/collapse":
        "同上，没有分组可折叠",
    "/v1/networks/{networkId}/groups/{groupNodeId}/expand":
        "同上，没有分组可展开",
    "/v1/networks/{networkId}/nodes/{nodeId}/pointer":
        "pointer 是「嵌套网络的 SUID」；该节点没有嵌套网络",
    # 注意模板是 `{objectType}/{objectId}`（不是 `{nodeId}`）——照对象级 bypass 走
    "/v1/networks/{networkId}/views/{viewId}/{objectType}/{objectId}/{visualProperty}/bypass":
        "该对象在该视觉属性上没有 bypass",
    "/v1/networks/{networkId}/views/{viewId}/network/{visualProperty}/bypass":
        "该网络在该视觉属性上没有 bypass",
}
# 另注：`/v1/styles/{name}/mappings/{vp}` 在「该 VP 没有映射」时也返回 404（语义正确）。
# 这里不登记它 —— 我们探测时喂的是真的有映射的 VP（NODE_LABEL），它会正常 200。

#: 上游对「输入不适用」的情况回 500 而非 4xx —— 属**上游缺陷**（docs/08 §7.4），
#: 不计入我们的失败。每条附「同一能力在别处正常」的反证。
UPSTREAM_500 = {
    "/v1/collections/{networkId}/tables/{tableType}/columns":
        "同一能力在 /v1/networks/{networkId}/tables/{tableType}/columns 下 200；"
        "collections 变体无论 defaultnode / defaultnetwork 恒 500",
    "/v1/styles/visualproperties/{vp}/values":
        "离散型 VP（NODE_SHAPE）→ 200 且返回枚举值；连续型（NODE_FILL_COLOR）→ 500，"
        "本应是 4xx",
}


def cli(*argv):
    return subprocess.run([PY, str(CLI), *argv, "--timeout", "60"],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=180, cwd=str(ROOT))


def rest_get(op):
    p = cli("rest", "GET", op)
    try:
        return json.loads(p.stdout)
    except Exception:  # noqa: BLE001
        return {"ok": None, "parse_error": p.stdout[:120]}


def _ext(path: str) -> str:
    """取端点路径的扩展名（`.csv` / `.png` …），没有则空串。"""
    tail = path.rsplit("/", 1)[-1]
    return ("." + tail.rsplit(".", 1)[1].lower()) if "." in tail else ""


def check_artifact(path, want_ext):
    """校验落盘产物：存在、非空、魔数匹配。返回 (ok, 描述)。"""
    p = Path(path)
    if not p.exists():
        return False, "产物未生成"
    raw = p.read_bytes()
    if not raw:
        return False, "产物为空"
    prefixes = MAGIC.get(want_ext)
    if prefixes and not raw.startswith(prefixes):
        return False, "魔数不符（%r 开头）" % raw[:8]
    return True, "%d 字节，魔头正确" % len(raw)


def discover_ids(port_root):
    """现场导入官方示例网络，并取齐所有需要的真实 ID / 合法参数值。

    **这里是第一版出错的地方**：随手拿 `networks` 里最后一个 SUID 当网络 ID，
    但 `collections/*` 系列要求的是**根网络** SUID（实测两者不同），
    于是 3 条本来正常的端点被记成 500；`properties` 的命名空间也一样，
    `network` 根本不在合法清单里。所以下面每一项都从引擎自己的枚举端点取。
    """
    sif = ROOT / "data" / "galFiltered.sif"
    assert sif.exists(), sif
    d = json.loads(cli("cmd", 'network import file file="%s"' % sif.as_posix()).stdout)
    assert d.get("ok"), d

    net = str(rest_get("networks")["result"][-1])
    # 根网络：`/v1/collections` 的语义就是「一个或全部根网络」
    roots = rest_get("collections")["result"]
    root = str(roots[0])
    view = str(rest_get("networks/%s/views" % net)["result"][0])
    nodes = rest_get("networks/%s/nodes" % net)["result"]
    edges = rest_get("networks/%s/edges" % net)["result"]
    rows = rest_get("networks/%s/tables/defaultnode/rows" % net)["result"]
    styles = rest_get("styles")["result"]
    layouts = rest_get("apply/layouts")["result"]
    panels = rest_get("ui/panels")["result"]

    # properties 的合法命名空间 + 该命名空间下的一个真实 key
    prop_ns = rest_get("properties")["result"]["data"]
    ns_pick = "vizmapper" if "vizmapper" in prop_ns else prop_ns[0]
    prop_keys = rest_get("properties/%s" % ns_pick)["result"]["data"]

    return {
        "net": net, "root": root, "view": view,
        "node": str(nodes[0]), "edge": str(edges[0]),
        "pk": str(rows[0]["SUID"]),
        "style": "default" if "default" in styles else styles[0],
        "layout": "force-directed" if "force-directed" in layouts else layouts[0],
        "panel": panels[0]["name"] if panels else "WEST",
        "prop_ns": ns_pick, "prop_key": prop_keys[0],
        "port_root": port_root,
        "counts": {"nodes": len(nodes), "edges": len(edges)},
    }


def fill(path, ids):
    """把 swagger 的 URI 模板填成真实路径；模板里还有没填上的洞就返回 missing=True。

    注意 `{namespace}` 与 `{visualProperty}` / `{vp}` 在不同路径下语义不同，
    必须按路径前缀分别取值 —— 一律填同一个值会制造一堆假的 404/500。
    """
    # 对象级路径（nodes/{nodeId} 或 {objectType}/{objectId}）用节点型 VP；
    # 网络层（views/{viewId}/network/{vp}）只认网络型 VP，
    # 拿节点型 VP 去问网络层会 404 —— 那是引擎语义正确，不是工具坏了。
    node_level = ("{objectType}" in path) or ("/nodes/{nodeId}" in path)
    ns = "layout" if path.startswith("/v1/commands/") else ids["prop_ns"]
    sub = {
        "networkId": ids["root"] if path.startswith("/v1/collections/") else ids["net"],
        "networkSUID": ids["net"], "suid": ids["net"],
        "viewId": ids["view"], "networkViewSUID": ids["view"],
        # help 端点：真实命令是 `layout get preferred`（不是 getLayoutNames，见 docs/08 §7.7）
        "namespace": ns, "command": "get preferred",
        "algorithmName": ids["layout"], "styleName": ids["style"], "name": ids["style"],
        "tableType": "defaultnode", "columnName": "name", "primaryKey": ids["pk"],
        "nodeId": ids["node"], "edgeId": ids["edge"], "objectId": ids["node"],
        "objectType": "nodes",
        # `{type}` 是边的 source/target，不是属性名
        "type": "source",
        # 网络层只认网络型 VP；拿节点型 VP 去问网络层会 404（引擎语义正确）
        "visualProperty": "NODE_FILL_COLOR" if node_level else "NETWORK_BACKGROUND_PAINT",
        # `values` 只对离散型 VP 有意义；`mappings` 要挑一个真的配了映射的 VP
        "vp": "NODE_SHAPE" if path.endswith("/values") else "NODE_LABEL",
        "vpName": "NODE_SHAPE", "panelName": ids["panel"],
        "groupNodeId": ids["node"], "propertyKey": ids["prop_key"],
    }
    out = path
    for k, v in sub.items():
        out = out.replace("{%s}" % k, v)
    return out, ("{" in out)


def probe(op, accept=None, base=None, out=None):
    """探一个端点。`out` 非空时走 `cyctl rest --out` 的字节通道。

    两条通道的判据不同：
      - 文本通道：退出码 ∈ {0,2,3,4,5}，stdout 是**一个** JSON 对象，`ok` 为真。
      - 字节通道：`ok` 为真 **且** 落盘产物通过魔数校验 ——
        只报 `ok:true` 不算数，产物本身要是那个格式（这是 §6.1 那个坑的教训）。
    """
    argv = [PY, str(CLI), "rest", "GET", op]
    if accept:
        argv += ["--accept", accept]
    if base:
        argv += ["--base-url", base]
    if out:
        argv += ["--out", out]
    argv += ["--timeout", "60"]
    t0 = time.time()
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180, cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        return {"rc": None, "json": False, "ok": None, "status": None,
                "note": "TIMEOUT", "secs": round(time.time() - t0, 2)}
    secs = round(time.time() - t0, 2)
    rec = {"rc": p.returncode, "json": False, "ok": None, "status": None,
           "note": "", "secs": secs, "channel": "bytes" if out else "text"}
    if p.returncode not in (0, 2, 3, 4, 5):
        rec["note"] = "退出码违反契约 rc=%s" % p.returncode
    try:
        d = json.loads(p.stdout)
        rec["json"] = isinstance(d, dict)
        rec["status"] = d.get("http_status")
        rec["ok"] = d.get("ok")
        if out:
            rec["size"] = d.get("size")
            rec["sha256"] = d.get("sha256")
            if d.get("ok"):
                good, desc = check_artifact(out, _ext(op))
                rec["artifact_ok"] = good
                rec["note"] = desc
            else:
                rec["artifact_ok"] = False
                rec["note"] = "HTTP %s: %s" % (
                    d.get("http_status"), str(d.get("body") or d.get("error"))[:80])
        elif d.get("ok") is False:
            rec["note"] = "HTTP %s: %s" % (d.get("http_status"),
                                           str(d.get("body") or d.get("error"))[:80])
        else:
            rec["note"] = "%d 字节" % len(json.dumps(d.get("result"), ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        rec["note"] = "stdout 非 JSON: %s | %r" % (exc, p.stdout[:70])
    return rec


def verdict_of(raw, rec):
    """给一条结果定级。顺序要紧：先认「声明跳过」，再认「语义 404」，最后才是失败。"""
    if raw in SKIP:
        return "declared_skip"
    if rec.get("rc") == 0 and rec.get("ok") is True and rec.get("artifact_ok", True):
        return "ok"
    if raw in EXPECT_ABSENT and rec.get("status") == 404:
        return "expected_absent"
    if raw in UPSTREAM_500 and rec.get("status") == 500:
        return "upstream_500"
    return "failed"


def main(argv=None):
    ap = argparse.ArgumentParser(description="REST 面逐端点探测")
    ap.add_argument("--port-root", default=DEFAULT_PORT_ROOT,
                    help="引擎端口根（默认 %(default)s）")
    ap.add_argument("--swagger", default=str(SWAGGER), help="swagger 快照路径")
    ap.add_argument("--out", default=str(OUT), help="证据输出路径")
    args = ap.parse_args(argv)

    port_root = args.port_root.rstrip("/")
    sw = Path(args.swagger)
    if not sw.exists():
        print("[audit] 缺 swagger 快照：%s" % sw, file=sys.stderr)
        print("[audit] 先取：cyctl rest GET swagger.json --out %s" % sw, file=sys.stderr)
        return 3

    ids = discover_ids(port_root)
    print("[audit] 现场 ID:", json.dumps(
        {k: v for k, v in ids.items() if k != "port_root"}, ensure_ascii=False))
    assert ids["counts"] == {"nodes": 330, "edges": 359}, ids["counts"]

    paths = json.loads(sw.read_text(encoding="utf-8"))["paths"]
    gets = sorted(p for p, ms in paths.items() if "get" in ms)
    print("[audit] GET 端点总数: %d\n" % len(gets))

    results, tally = [], {}
    for raw in gets:
        if raw in SKIP:
            results.append({"path": raw, "verdict": "declared_skip",
                            "skipped": SKIP[raw]})
            tally["declared_skip"] = tally.get("declared_skip", 0) + 1
            print("SKIP  %-58s %s" % (raw, SKIP[raw]))
            continue
        filled, missing = fill(raw, ids)
        if missing:
            results.append({"path": raw, "verdict": "declared_skip",
                            "skipped": "URI 模板无法填充"})
            tally["declared_skip"] = tally.get("declared_skip", 0) + 1
            print("SKIP  %-58s 模板缺参" % raw)
            continue

        # cyctl rest 自己会拼 /v1；非 /v1 的 App 端点要改用端口根做 base。
        if filled.startswith("/v1"):
            op, base = filled[3:].lstrip("/"), None
        else:
            op, base = filled.lstrip("/"), port_root

        ext = _ext(filled)
        accept, out = ACCEPT.get(raw), None
        if ext in BINARY_FORMAT:
            # 二进制端点必须走字节通道 —— Accept 协商与不可逆解码损坏两个坑都在这里。
            out = str(ARTS / ("art_%d%s" % (len(results), ext)))
            accept = None                    # --out 默认 Accept: */*

        rec = probe(op, accept, base, out)
        rec.update({"path": raw, "op": op, "mutates": raw in MUTATING,
                    "accept": accept})
        rec["verdict"] = verdict_of(raw, rec)
        tally[rec["verdict"]] = tally.get(rec["verdict"], 0) + 1
        results.append(rec)

        mark = {"ok": "OK  ", "expected_absent": "ABS ", "upstream_500": "UP5 ",
                "failed": "BAD "}[rec["verdict"]]
        print("%s %-52s %-5s rc=%-2s http=%-5s %5ss %s" % (
            mark, op[:52], rec.get("channel"), rec.get("rc"), rec.get("status"),
            rec.get("secs"), rec.get("note")))

    probed = [r for r in results if "op" in r]
    failed = [r for r in probed if r["verdict"] == "failed"]
    by_channel = {}
    for r in probed:
        c = by_channel.setdefault(r.get("channel", "text"), {"probed": 0, "failed": 0})
        c["probed"] += 1
        c["failed"] += (r["verdict"] == "failed")

    print("\n" + "=" * 80)
    print("[audit] GET 端点 %d 条：探了 %d，声明不探 %d" % (
        len(gets), len(probed), tally.get("declared_skip", 0)))
    for k in ("ok", "expected_absent", "upstream_500", "failed", "declared_skip"):
        if k in tally:
            print("    %-16s %3d" % (k, tally[k]))
    for ch, c in sorted(by_channel.items()):
        print("    通道 %-6s 探 %3d，失败 %d" % (ch, c["probed"], c["failed"]))
    if failed:
        for r in failed:
            print("  BAD %-50s rc=%s http=%s : %s" % (r["op"][:50], r.get("rc"),
                                                      r.get("status"), r.get("note")))
    else:
        print("[audit] 失败 0 —— 没有任何一条是「因为工具自身写错而跑不了」")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({
            "port_root": port_root, "base": port_root + "/v1",
            "engine_ids": ids, "total_get_endpoints": len(gets),
            "probed": len(probed), "declared_skip": tally.get("declared_skip", 0),
            "tally": tally, "failed": len(failed),
            "by_channel": by_channel,
            "expect_absent_reasons": EXPECT_ABSENT,
            "upstream_500_reasons": UPSTREAM_500,
            "results": results,
        }, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print("[audit] 已写入: %s (%d 字节)" % (out, os.path.getsize(out)))

    shutil.rmtree(ARTS, ignore_errors=True)
    return 4 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
