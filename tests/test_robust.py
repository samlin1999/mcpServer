"""畸形與惡意輸入的回歸測試。

模型給的參數型別完全不保證：少給、給錯型別、給超大的數字、給控制字元。
這裡驗的是三個「壞掉就是災難」的性質：

1. **伺服器不能死。** 一則壞訊息讓行程結束的話，客戶端後續每個請求都等不到回覆，
   而且看不出原因——症狀是整個 MCP 連線無聲失效。
2. **stdout 只能有 JSON-RPC。** 混進任何雜訊就把協定弄壞。
3. **每個錯誤都要是「使用者做得到的修正」。** 掉進 server 的 catch-all 就只會顯示
   一句「未預期的錯誤：TypeError: …」，模型看不出自己哪裡寫錯，只會原樣重試。

用法：python tests/test_robust.py
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


def check(name: str, condition, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"[{'  ok  ' if condition else ' FAIL '}] {name}"
          + (f"  — {detail}" if detail and not condition else ""))


def run_session(raw_lines: list) -> tuple:
    """把一整批原始輸入餵給伺服器，回傳 (依 id 索引的回覆, 離開碼, stderr)。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=ROOT)
    proc = subprocess.Popen([sys.executable, "-m", "csdmcp"], cwd=ROOT, env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    body = "".join(line + "\n" for line in raw_lines).encode("utf-8")
    out, err = proc.communicate(body, timeout=600)

    replies, junk = {}, []
    for line in out.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            junk.append(line[:120])
            continue
        replies[repr(message.get("id"))] = message
    return replies, proc.returncode, err.decode("utf-8", "replace"), junk


def main() -> int:
    real = sorted(projects.index_root(projects.roots()[0]).items())[0][1]

    serial = [100]

    def call(tool: str, args) -> tuple:
        serial[0] += 1
        return serial[0], json.dumps(
            {"jsonrpc": "2.0", "id": serial[0], "method": "tools/call",
             "params": {"name": tool, "arguments": args}}, ensure_ascii=False)

    hello = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})

    # ------------------------------------------------------------------ 1. 非物件訊息
    # 一行合法 JSON 但不是物件（批次請求、純量）曾經直接讓行程結束：
    # handle() 的 message.get 爆掉，而 catch-all 自己也呼叫 message.get，一起爆。
    probe = [hello]
    for junk_line in ("[1, 2, 3]", "42", '"字串"', "null", "true", "[]", "{}"):
        probe.append(junk_line)
        probe.append(json.dumps({"jsonrpc": "2.0", "id": 200 + len(probe),
                                 "method": "ping"}))
    probe += ["這一行不是 JSON", "{ 這行也不是 }", "",
              json.dumps({"jsonrpc": "2.0", "id": 299, "method": "ping"})]
    replies, code, err, junk = run_session(probe)
    pings = [i for i in range(200, 300) if repr(i) in replies]
    check("非物件訊息不會讓伺服器結束", repr(299) in replies,
          f"只回了 {len(pings)} 個 ping")
    check("非物件訊息之後每個 ping 都有回", len(pings) == 8, f"{len(pings)} / 8")
    check("離開碼是 0", code == 0, str(code))
    check("stdout 沒有雜訊", not junk, str(junk[:2]))
    check("stderr 沒有 traceback", "Traceback" not in err,
          err[err.find("Traceback"):][:200] if "Traceback" in err else "")

    # ------------------------------------------------------------------ 2. params / arguments 的型別
    cases = [
        (2001, {"jsonrpc": "2.0", "id": 2001, "method": "tools/call", "params": "字串"}),
        (2002, {"jsonrpc": "2.0", "id": 2002, "method": "tools/call", "params": [1, 2]}),
        (2003, {"jsonrpc": "2.0", "id": 2003, "method": "tools/call",
                "params": {"name": "info", "arguments": "不是物件"}}),
        (2004, {"jsonrpc": "2.0", "id": 2004, "method": "tools/call",
                "params": {"name": "info", "arguments": [1, 2]}}),
    ]
    replies, code, err, junk = run_session(
        [hello] + [json.dumps(m, ensure_ascii=False) for _i, m in cases])
    for ident in (2001, 2002):
        got = replies.get(repr(ident)) or {}
        check(f"params 型別錯回 -32602（id={ident}）",
              (got.get("error") or {}).get("code") == -32602, json.dumps(got)[:120])
    for ident in (2003, 2004):
        got = (replies.get(repr(ident)) or {}).get("result") or {}
        text = (got.get("content") or [{}])[0].get("text", "")
        check(f"arguments 型別錯回 isError（id={ident}）", got.get("isError") is True)
        check(f"訊息說得出 arguments 要是物件（id={ident}）",
              "arguments" in text and "物件" in text, text[:120])

    # ------------------------------------------------------------------ 3. 工具參數的型別與缺漏
    probe, expect = [hello], []
    for tool in ("info", "tree", "find", "spec", "doctor", "render", "compare",
                 "list_anim", "set_anim", "create_csd", "register_csd", "set_prop",
                 "add_node", "delete_node", "duplicate_node", "reparent",
                 "edit_batch", "history", "undo", "list_csd", "add_project"):
        ident, line = call(tool, {})
        probe.append(line)
        expect.append((f"{tool}(空參數)", ident))

    bad_args = [
        ("csd 是數字", "info", {"csd": 12345}),
        ("csd 是陣列", "info", {"csd": ["a"]}),
        ("csd 是物件", "info", {"csd": {"x": 1}}),
        ("不存在的檔", "info", {"csd": "C:/根本/沒有/這個.csd"}),
        ("目錄當檔案", "info", {"csd": ROOT}),
        ("路徑穿越", "info", {"csd": "../../../../../../etc/passwd"}),
        ("非 .csd 檔", "info", {"csd": os.path.join(ROOT, "README.md")}),
        ("超長路徑", "info", {"csd": "C:/" + "a" * 5000 + ".csd"}),
        ("NUL 字元路徑", "info", {"csd": "C:/x\x00y.csd"}),
        ("depth 是字串", "tree", {"csd": real, "depth": "深一點"}),
        ("depth 是負數", "tree", {"csd": real, "depth": -5}),
        ("depth 超大", "tree", {"csd": real, "depth": 10 ** 9}),
        ("frame 是 NaN 字串", "tree", {"csd": real, "frame": "NaN"}),
        ("scale 是 0", "render", {"csd": real, "scale": 0}),
        ("scale 是負數", "render", {"csd": real, "scale": -3}),
        ("props 是字串", "set_prop", {"csd": real, "node": "Node", "props": "dx=1"}),
        ("props 是陣列", "set_prop", {"csd": real, "node": "Node", "props": [1, 2]}),
        ("props 是數字", "set_prop", {"csd": real, "node": "Node", "props": 7}),
        ("ops 是字串", "edit_batch", {"csd": real, "ops": "隨便"}),
        ("ops 裡是 null", "edit_batch", {"csd": real, "ops": [None]}),
        ("ops 裡是字串", "edit_batch", {"csd": real, "ops": ["set"]}),
        ("ops 裡是數字", "edit_batch", {"csd": real, "ops": [1, 2]}),
        ("steps 是字串", "undo", {"csd": real, "steps": "一步"}),
        ("steps 是負數", "undo", {"csd": real, "steps": -3}),
        ("steps 超大", "undo", {"csd": real, "steps": 10 ** 9}),
        ("節點名有控制字元", "add_node", {"csd": real, "name": "a\x01b", "type": "text"}),
        ("文字有控制字元", "add_node",
         {"csd": real, "type": "text", "name": "t", "props": {"text": "壞\x0bの"}}),
        ("節點路徑是純斜線", "set_prop", {"csd": real, "node": "///", "props": {"dx": 1}}),
        ("節點名超長", "find", {"csd": real, "name": "x" * 10000}),
        ("emoji 節點名", "find", {"csd": real, "name": "🙂🙃"}),
        ("type 亂給", "add_node", {"csd": real, "type": "不存在的型別", "name": "t"}),
        ("未知工具", "根本沒這個工具", {}),
    ]
    for label, tool, args in bad_args:
        ident, line = call(tool, args)
        probe.append(line)
        expect.append((label, ident))
    probe.append(json.dumps({"jsonrpc": "2.0", "id": 9999, "method": "ping"}))

    replies, code, err, junk = run_session(probe)
    check("壞參數轟炸後伺服器還活著", repr(9999) in replies)
    check("壞參數轟炸後離開碼是 0", code == 0, str(code))
    check("壞參數轟炸不產生 stdout 雜訊", not junk, str(junk[:2]))

    silent = [lab for lab, ident in expect if repr(ident) not in replies]
    check("每個壞請求都有回覆", not silent, f"{len(silent)} 個沒回：{silent[:5]}")

    catch_all, protocol_err = [], []
    for label, ident in expect:
        got = replies.get(repr(ident))
        if got is None:
            continue
        if "error" in got:
            protocol_err.append(label)
            continue
        body = json.dumps(got.get("result") or {}, ensure_ascii=False)
        if "未預期的錯誤" in body:
            catch_all.append(f"{label}: {body[body.index('未預期的錯誤'):][:70]}")
    check("沒有任何壞參數掉進 catch-all", not catch_all,
          f"{len(catch_all)} 個 -> " + " | ".join(catch_all[:4]))
    check("壞參數不會變成協定層 error", not protocol_err, str(protocol_err[:4]))
    check("壞參數轟炸不留 traceback", "Traceback" not in err,
          err[err.find("Traceback"):][:200] if "Traceback" in err else "")


    # ------------------------------------------------------------------ 4. 畸形的 .csd 檔
    # 檔案不一定是 Cocos Studio 存出來的：可能被 git 衝突標記污染、被截斷、
    # 存成 UTF-16，或根本是別的東西改了副檔名。每一種都要給得出可行動的錯誤。
    stage = tempfile.mkdtemp(prefix="csdmcp_bad_")
    try:
        base = open(real, "rb").read()
        broken = {
            "empty": b"",
            "whitespace": b"   \r\n\t  ",
            "binary": bytes(range(256)) * 40,
            "truncated": base[:len(base) // 2],
            "no_close": base.replace(b"</GameFile>", b""),
            "double_root": base + b"\r\n" + base,
            "git_conflict": (base.replace(b"<Content ctype=",
                                          b"<<<<<<< HEAD\r\n<Content ctype=", 1)
                             + b"\r\n=======\r\n>>>>>>> theirs\r\n"),
            "utf16": base.decode("utf-8").encode("utf-16"),
            "not_gamefile": b"<Other><Thing /></Other>",
            "unclosed_attr": base.replace(b'ctype="GameProjectContent"',
                                          b'ctype="GameProjectContent', 1),
            "bad_entity": base.replace(b"<Content", b'<Content Note="&nosuch;"', 1),
            "nul_inside": base[:500] + b"\x00" + base[500:],
            # 上千層巢狀曾經是 RecursionError——解譯器層級的錯誤翻不成有用的訊息
            "deep_nest": b"<GameFile>" + b"<A>" * 3000 + b"</A>" * 3000 + b"</GameFile>",
        }
        paths = {}
        for name, data in broken.items():
            paths[name] = os.path.join(stage, name + ".csd")
            with open(paths[name], "wb") as fh:
                fh.write(data)

        probe, expect = [hello], []
        for name, path in sorted(paths.items()):
            for tool, extra in (("info", {}), ("tree", {}), ("doctor", {}),
                                ("spec", {}), ("list_anim", {}),
                                ("set_prop", {"node": "Node", "props": {"dx": 1}}),
                                ("add_node", {"type": "text", "name": "probe"}),
                                ("undo", {})):
                ident, line = call(tool, {"csd": path, **extra})
                probe.append(line)
                expect.append((f"{name}/{tool}", ident))
        probe.append(json.dumps({"jsonrpc": "2.0", "id": 8888, "method": "ping"}))

        snapshot = {n: open(p, "rb").read() for n, p in paths.items()}
        replies, code, err, junk = run_session(probe)

        check("畸形檔轟炸後伺服器還活著", repr(8888) in replies)
        check("畸形檔轟炸後離開碼是 0", code == 0, str(code))
        check("畸形檔不產生 stdout 雜訊", not junk, str(junk[:2]))
        check("畸形檔轟炸不留 traceback", "Traceback" not in err,
              err[err.find("Traceback"):][:200] if "Traceback" in err else "")

        silent = [lab for lab, ident in expect if repr(ident) not in replies]
        check("每個畸形檔呼叫都有回覆", not silent, f"{len(silent)} 個：{silent[:5]}")

        leaked = []
        for label, ident in expect:
            got = replies.get(repr(ident))
            if got is None:
                continue
            body = json.dumps(got.get("result") or got, ensure_ascii=False)
            if "未預期的錯誤" in body:
                leaked.append(f"{label} -> " + body[body.index("未預期的錯誤"):][:60])
        check("沒有畸形檔掉進 catch-all", not leaked,
              f"{len(leaked)} 個 -> " + " | ".join(leaked[:4]))

        # 解析不了的檔，寫入路徑一個位元組都不能碰
        untouched = [n for n in paths
                     if open(paths[n], "rb").read() != snapshot[n]]
        check("解析失敗的檔沒有被寫壞", not untouched, str(untouched))

        # 錯誤訊息要說得出原因，不能只有 expat 的行號
        hints = {
            "git_conflict": "git",
            "utf16": "UTF-16",
            "empty": "空",
        }
        for name, needle in hints.items():
            ident = next(i for lab, i in expect if lab == f"{name}/set_prop")
            text = json.dumps((replies.get(repr(ident)) or {}).get("result") or {},
                              ensure_ascii=False)
            check(f"{name} 的錯誤說得出原因（提到「{needle}」）", needle in text,
                  text[:160])
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    # ------------------------------------------------------------------ 5. 格式不標準但合法的檔
    # 這些是「能編輯」的，重點是編完不能把格式弄掉——換行風格、BOM、XML 宣告
    # 只要變一個，Cocos Studio 下次存檔就會重排整份檔案，git 上變成一大包 diff。
    stage = tempfile.mkdtemp(prefix="csdmcp_fmt_")
    try:
        from csdmcp import tools as tools_mod
        base = open(real, "rb").read()
        odd = {
            "bom": b"\xef\xbb\xbf" + base,
            "lf_only": base.replace(b"\r\n", b"\n"),
            "xml_decl": b'<?xml version="1.0" encoding="UTF-8"?>\r\n' + base,
        }
        for name, data in odd.items():
            path = os.path.join(stage, name + ".csd")
            with open(path, "wb") as fh:
                fh.write(data)
            had_bom = data.startswith(b"\xef\xbb\xbf")
            had_decl = b"<?xml" in data[:60]
            crlf_style = data.count(b"\r\n") > 0
            tools_mod.call("set_prop", {"csd": path, "node": "Node",
                                        "props": {"dx": 3}})
            after = open(path, "rb").read()
            check(f"{name}：編輯後 BOM 沒變",
                  after.startswith(b"\xef\xbb\xbf") == had_bom)
            check(f"{name}：編輯後 XML 宣告沒變",
                  (b"<?xml" in after[:60]) == had_decl)
            check(f"{name}：編輯後換行風格沒變",
                  (after.count(b"\r\n") > 0) == crlf_style,
                  f"CRLF {data.count(b'.'):d}")
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    print(f"\n通過 {len(PASS)}，失敗 {len(FAIL)}")
    if FAIL:
        print("失敗項目：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
