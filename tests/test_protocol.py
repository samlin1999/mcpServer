"""把伺服器當成真的 MCP 客戶端來跑一遍。

直接開子行程、走 stdin/stdout，驗的是「客戶端接上去會不會動」，
而不是「函式呼叫得通」——協定層的錯（stdout 混進雜訊、initialize 回錯版本）
只有這樣測才看得出來。

用法：python tests/test_protocol.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from csdmcp import projects                       # noqa: E402

PASS, FAIL = [], []

NEWLINE = chr(10)


def check(name: str, condition, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"[{'  ok  ' if condition else ' FAIL '}] {name}"
          + (f"  — {detail}" if detail and not condition else ""))


class Client:
    """最小的 MCP 客戶端：一行一則 JSON。"""

    def __init__(self, cwd: str = ROOT):
        # PYTHONPATH 要指回專案根：工作目錄不一定是根（見「compare 不污染工作目錄」那段）
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=ROOT)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "csdmcp"], cwd=cwd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.serial = 0

    def send(self, method: str, params=None, notify: bool = False):
        message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self.serial += 1
            message["id"] = self.serial
        self.proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        if notify:
            return None
        line = self.proc.stdout.readline()
        if not line:
            raise SystemExit("伺服器沒有回應（stderr: "
                             + self.proc.stderr.read().decode("utf-8", "replace")[-800:] + "）")
        return json.loads(line.decode("utf-8"))

    def call(self, name: str, args: dict):
        reply = self.send("tools/call", {"name": name, "arguments": args})
        return reply.get("result", reply)

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


def payload_of(result: dict):
    for item in result.get("content", []):
        if item.get("type") == "text":
            try:
                return json.loads(item["text"])
            except ValueError:
                return item["text"]
    return None


def check_output_is_compact() -> None:
    """回給模型的 JSON 必須是緊湊的（沒有縮排）。

    這是 token 成本的守門員，不是美觀問題：縮排對解析毫無幫助，但很貴。
    實測全庫最大的那個 .csd，tree 的輸出加上縮排會從 8.6 萬字元漲到 29 萬
    （多 71%），一次呼叫就多花五萬個 token。

    順帶一提，縮排也不會讓 JSON-RPC 那一行變短——換行在字串裡會被轉義，
    所以緊湊反而讓單行更短，對協定是往安全的方向走。
    """
    from csdmcp import server

    sample = {"nodes": [{"name": "節點", "size": [120.0, 41.0],
                         "children": [{"name": "子", "size": [1.0, 2.0]}]}]}
    body = server._text(sample)["text"]

    check("回給模型的 JSON 沒有縮排",
          NEWLINE not in body and ": " not in body, repr(body[:80]))
    check("緊湊輸出沒有弄丟資料", json.loads(body) == sample)
    check("中文沒有被轉義", "節點" in body, repr(body[:80]))

    indented = json.dumps(sample, ensure_ascii=False, indent=2)
    check("確實比縮排版短", len(body) < len(indented),
          f"{len(body)} vs {len(indented)}")

    # 字串型的回傳（錯誤訊息）要原樣通過，換行不能被吃掉
    message = "錯誤：找不到節點。" + NEWLINE + "請先用 tree 取得路徑。"
    check("字串回傳原樣通過", server._text(message)["text"] == message)


def main() -> int:
    client = Client()

    reply = client.send("initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"}})
    result = reply.get("result", {})
    check("initialize 有回應", bool(result))
    check("回報的協定版本正確", result.get("protocolVersion") == "2025-06-18",
          str(result.get("protocolVersion")))
    check("宣告了 tools 能力", "tools" in (result.get("capabilities") or {}))
    client.send("notifications/initialized", notify=True)

    listing = client.send("tools/list").get("result", {}).get("tools", [])
    names = {t["name"] for t in listing}
    check("工具清單拿得到", len(listing) >= 15, f"只有 {len(listing)} 個")
    for needed in ("render", "set_prop", "add_node", "delete_node", "reparent",
                   "edit_batch", "undo", "doctor", "list_projects",
                   "set_anim", "list_anim"):
        check(f"有 {needed} 工具", needed in names)
    check("每個工具都有 inputSchema",
          all(isinstance(t.get("inputSchema"), dict) for t in listing))

    check("舊版協定也接受",
          client.send("initialize", {"protocolVersion": "2024-11-05"})
          .get("result", {}).get("protocolVersion") == "2024-11-05")

    projects_result = payload_of(client.call("list_projects", {}))
    check("list_projects 回得出專案", isinstance(projects_result, dict)
          and projects_result.get("count", 0) >= 1)
    check("有偵測到 csdview", (projects_result.get("csdview") or {}).get("ok") is True,
          str(projects_result.get("csdview")))

    # ---- 準備一份可以放心亂改的檔案
    root = projects.roots()[0]
    source = None
    for _rel, full in sorted(projects.index_root(root).items()):
        if "DailyDiscount.csd" in full:
            source = full
            break
    source = source or list(projects.index_root(root).values())[0]
    work = os.path.join(os.path.dirname(source), "__csdmcp_proto__.csd")
    shutil.copy2(source, work)

    try:
        tree = payload_of(client.call("tree", {"csd": work, "depth": 2}))
        check("tree 讀得到節點", isinstance(tree, dict) and tree.get("tree"))

        # 挑一個名稱在整份檔案裡唯一的節點，才不會一開始就撞上同名歧義
        kids = tree["tree"]["children"]
        names = [k["path"].split("/")[-1] for k in kids]
        node = next(k["path"] for k in kids
                    if names.count(k["path"].split("/")[-1]) == 1)
        leaf = node.split("/")[-1]
        print(f"  測試節點：{node}")

        # 參照另一個節點量相對位移。screenRect 的原點是「完整內容外框」的左上角，
        # 移動最外圍的節點會把外框一起帶著跑，拿它的絕對值當基準會量錯。
        other = next(k["path"].split("/")[-1] for k in kids
                     if k["path"] != node and names.count(k["path"].split("/")[-1]) == 1)

        def probe():
            mine = payload_of(client.call("find", {"csd": work, "name": leaf}))["matches"][0]
            ref = payload_of(client.call("find", {"csd": work, "name": other}))["matches"][0]
            return mine["position"][0], mine["screenRect"][0] - ref["screenRect"][0]

        pos_before, gap_before = probe()

        # 同名節點一定要報歧義，不能隨便挑一個改
        duplicated = next((n for n in names if names.count(n) > 1), None)
        if duplicated:
            clash = client.call("set_prop", {"csd": work, "node": f"Node/{duplicated}",
                                             "props": {"dx": 1}})
            check("同名節點會要求指定序號",
                  clash.get("isError") is True
                  and "對應到" in clash["content"][0]["text"],
                  clash["content"][0]["text"][:120])

        moved = payload_of(client.call("set_prop", {
            "csd": work, "node": node, "props": {"dx": 25, "dy": 0}}))
        check("set_prop 有寫檔", moved.get("written") is True, json.dumps(moved)[:200])

        pos_after, gap_after = probe()
        check("節點座標移動了 25", abs((pos_after - pos_before) - 25) < 0.01,
              f"{pos_before} -> {pos_after}")
        check("渲染出來的相對位置也移動了 25px",
              abs((gap_after - gap_before) - 25) < 0.6,
              f"間距 {gap_before} -> {gap_after}")

        health = payload_of(client.call("doctor", {"csd": work}))
        check("編輯後沒有 staleLayout", not health["staleLayout"],
              json.dumps(health["staleLayout"], ensure_ascii=False)[:300])

        # ---- 回傳的 JSON 不能帶排版：縮排要花 token 又不帶資訊
        # tree 的 depth 也要真的生效（csdview 以前只讓它影響文字輸出）。
        shallow = client.call("tree", {"csd": work, "depth": 1})
        raw = shallow["content"][0]["text"]
        check("回傳的 JSON 沒有排版", "\n  " not in raw, raw[:80])
        check("沒有排版還是解析得動", isinstance(json.loads(raw), dict))
        deep = payload_of(client.call("tree", {"csd": work, "depth": 99}))
        thin = payload_of(shallow)

        def nodes(node):
            return 1 + sum(nodes(c) for c in node.get("children") or [])

        check("depth 真的有裁掉層數", nodes(thin["tree"]) < nodes(deep["tree"]),
              f"depth=1 {nodes(thin['tree'])} 個 / 全部 {nodes(deep['tree'])} 個")
        check("被裁掉的節點留下 childCount",
              any("childCount" in c for c in thin["tree"].get("children") or []),
              str(thin["tree"].get("children"))[:200])


        # ---- spec 的範圍篩選：整份在大檔上是幾萬個 token，要能只要一塊
        whole = payload_of(client.call("spec", {"csd": work}))
        check("不篩選時不該出現 totalNodeCount",
              "totalNodeCount" not in whole, str(whole.get("totalNodeCount")))

        shallow_spec = payload_of(client.call("spec", {"csd": work, "depth": 1}))
        check("spec 的 depth 有生效",
              shallow_spec["nodeCount"] < whole["nodeCount"],
              f"{shallow_spec['nodeCount']} / {whole['nodeCount']}")
        check("篩選過會回報整份有幾個節點",
              shallow_spec.get("totalNodeCount") == whole["nodeCount"],
              str(shallow_spec.get("totalNodeCount")))
        check("篩選過會回報用了什麼條件",
              (shallow_spec.get("filter") or {}).get("depth") == 1,
              str(shallow_spec.get("filter")))

        # 只要一棵子樹：內容要跟整份輸出裡的同一批節點逐一相同
        target = whole["nodes"][1]["path"]
        subtree = payload_of(client.call("spec", {"csd": work, "node": target}))

        def under(node):
            return node["path"] == target or node["path"].startswith(target + "/")

        check("node 篩出來的都在那棵子樹底下",
              all(under(n) for n in subtree["nodes"]),
              str([n["path"] for n in subtree["nodes"]])[:150])
        # 逐一比對要用「有序子集」而不是 path 索引：同名兄弟會讓路徑重複
        # （這批 .csd 裡真的有，例如四個 downFrame/Bg），拿 dict 索引會比錯對象。
        check("子樹節點的資料跟整份輸出完全一致",
              [n for n in whole["nodes"] if under(n)] == subtree["nodes"])
        # 篩選只能縮小輸出，不能改變座標系——否則拿子集比對會得到錯的位置
        check("篩選不會改變座標系",
              all(whole[k] == subtree[k] for k in
                  ("canvasSize", "imageSize", "scale", "worldOrigin", "designRect")))

        missing = client.call("spec", {"csd": work, "node": "根本沒有這個節點"})
        check("node 打錯會回 isError", missing.get("isError") is True)
        check("打錯時說得出怎麼找正確路徑",
              "tree" in missing["content"][0]["text"]
              or "接近" in missing["content"][0]["text"],
              missing["content"][0]["text"][:150])

        rendered = client.call("render", {"csd": work, "scale": 0.5})
        images = [c for c in rendered.get("content", []) if c.get("type") == "image"]
        check("render 直接回傳圖片", len(images) == 1)
        if images:
            check("圖片是 base64 PNG", len(images[0]["data"]) > 2000
                  and images[0]["mimeType"] == "image/png",
                  f"長度 {len(images[0].get('data', ''))}")

        preview = client.call("set_prop", {
            "csd": work, "node": node, "props": {"dx": -25}, "preview": True})
        check("preview 會附上渲染圖",
              any(c.get("type") == "image" for c in preview.get("content", [])))

        # ---- compare 的差異圖不能落在伺服器的工作目錄
        # 那個目錄就是使用者的 Creator 專案：csdview 沒收到 -o 時會寫成
        # out/<名稱>_diff.png，直接冒在人家的 git status 上。
        sandbox = tempfile.mkdtemp(prefix="csdmcp_cwd_")
        second = Client(cwd=sandbox)
        try:
            second.send("initialize", {"protocolVersion": "2025-06-18",
                                       "capabilities": {}, "clientInfo": {"name": "t"}})
            second.send("notifications/initialized", notify=True)
            shot = payload_of(second.call("render", {"csd": work}))
            diff = payload_of(second.call("compare", {"csd": work,
                                                      "reference": shot["image"]}))
            check("compare 跑得起來", isinstance(diff, dict) and "verdict" in diff,
                  str(diff)[:200])
            check("差異圖有寫出來", os.path.isfile(diff.get("diffImage") or ""),
                  str(diff.get("diffImage")))
            check("差異圖不在工作目錄裡",
                  not os.listdir(sandbox), str(os.listdir(sandbox)))
        finally:
            second.close()
            shutil.rmtree(sandbox, ignore_errors=True)

        batch = payload_of(client.call("edit_batch", {
            "csd": work,
            "ops": [
                {"op": "add", "type": "text", "name": "csdmcp_t1",
                 "props": {"x": 50, "y": 50, "w": 120, "h": 24, "text": "測試文字"}},
                {"op": "add", "type": "sprite", "name": "csdmcp_t2",
                 "props": {"x": 80, "y": 80, "w": 32, "h": 32}},
                {"op": "set", "node": "csdmcp_t1", "props": {"color": "#FF8800", "alpha": 200}},
            ]}))
        check("edit_batch 三個操作都成功", len(batch.get("operations", [])) == 3)
        check("edit_batch 只存一份快照", isinstance(batch.get("snapshot"), int))

        broken = client.call("edit_batch", {
            "csd": work,
            "ops": [{"op": "set", "node": "csdmcp_t2", "props": {"dx": 5}},
                    {"op": "set", "node": "根本不存在的節點", "props": {"dx": 5}}]})
        check("整批有一個錯就全部放棄", broken.get("isError") is True)
        still = payload_of(client.call("find", {"csd": work, "name": "csdmcp_t2"}))
        check("失敗的整批沒有留下副作用",
              abs(still["matches"][0]["position"][0] - 80) < 0.01,
              str(still["matches"][0]["position"]))

        undone = payload_of(client.call("undo", {"csd": work}))
        check("undo 還原得回去", undone.get("restored", {}).get("serial", 0) >= 1)
        gone = payload_of(client.call("find", {"csd": work, "name": "csdmcp_t1"}))
        check("undo 之後新節點不見了", gone["count"] == 0)


        # ---- 檔案被別的行程一直開著：客戶端要收到可行動的錯誤
        # 沒有翻譯的話這裡會是 server 的 catch-all，模型只看到
        # 「未預期的錯誤：PermissionError: [WinError 5]」，完全不知道下一步做什麼。
        keeper = open(work, "rb")
        try:
            blocked = client.call("set_prop", {"csd": work, "node": node,
                                               "props": {"dx": 1}})
            text = blocked["content"][0]["text"]
            check("換不進去時回 isError", blocked.get("isError") is True)
            check("換不進去時不是「未預期的錯誤」", "未預期" not in text, text[:200])
            check("換不進去時說得出要關掉誰", "關掉" in text, text[:200])
        finally:
            keeper.close()

        # ---- undo 沒有紀錄：一樣要是可行動的錯誤而不是 catch-all
        virgin = os.path.join(os.path.dirname(work), "__csdmcp_virgin__.csd")
        shutil.copy2(source, virgin)
        try:
            nothing = client.call("undo", {"csd": virgin})
            text = nothing["content"][0]["text"]
            check("沒有紀錄的 undo 回 isError", nothing.get("isError") is True)
            check("沒有紀錄的 undo 不是「未預期的錯誤」", "未預期" not in text, text[:200])
        finally:
            if os.path.exists(virgin):
                os.remove(virgin)

        bad = client.call("set_prop", {"csd": work, "node": "沒有這個節點",
                                       "props": {"dx": 1}})
        check("找不到節點會回 isError", bad.get("isError") is True)
        check("錯誤訊息說得出怎麼修",
              "tree" in bad["content"][0]["text"] or "find" in bad["content"][0]["text"])
    finally:
        for path in (work,):
            if os.path.exists(path):
                os.remove(path)
        client.close()

    check_output_is_compact()

    print(f"\n通過 {len(PASS)}，失敗 {len(FAIL)}")
    if FAIL:
        print("失敗項目：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
