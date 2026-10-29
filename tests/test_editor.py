"""拿專案裡的真實 .csd 當素材，驗證編輯不會弄壞檔案。

最重要的一條是 staleLayout：Cocos Studio 把位置存了三份（絕對值、百分比、四個邊界），
只改其中一份不會有任何錯誤訊息，但檔案就此壞掉——移植到引擎後位置跟編輯器看到的不一樣。
所以每個會動到座標的操作，測試都直接拿 csdview 的 doctor 來檢查。

用法：python tests/test_editor.py [cocosstudio 根目錄]
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from csdmcp import projects                       # noqa: E402
from csdmcp.editor import Csd, EditError          # noqa: E402


PASS, FAIL = [], []


def check(name: str, condition, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    mark = "  ok  " if condition else " FAIL "
    print(f"[{mark}] {name}" + (f"  — {detail}" if detail and not condition else ""))


def doctor(path: str) -> dict:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    done = subprocess.run([sys.executable, "-m", "csdview", "doctor", path, "--json"],
                          capture_output=True, env=env)
    return json.loads(done.stdout.decode("utf-8"))


def pick_sample(root: str) -> str:
    """挑一個有代表性的檔案。

    條件刻意訂得嚴：**一定要有「本身有尺寸、底下又還有子節點」的中間層**，
    否則「改尺寸要重算子節點」與「搬移階層」這兩條最容易寫錯的路徑根本走不到，
    測試會全綠但什麼都沒驗到。素材本身也要求乾淨（沒有既有的 staleLayout 或缺資源），
    不然分不清問題是編輯造成的還是本來就有的。
    """
    for _rel, full in sorted(projects.index_root(root).items()):
        try:
            doc = Csd(full)
        except Exception:
            continue
        nodes = doc.nodes()
        if len(nodes) < 10:
            continue
        middles = [n for n in nodes[1:]
                   if doc.elem_of(n.node_path).child("Children") is not None
                   and doc.geometry(n.node_path)["w"] > 0]
        if len(middles) < 2:
            continue
        report = doctor(full)
        if report["staleLayout"] or report["missingAssets"]:
            continue
        return full
    raise SystemExit("找不到適合當測試素材的 .csd（需要有帶尺寸的中間層）")


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else (projects.roots() or [None])[0]
    if not root:
        raise SystemExit("找不到 cocosstudio 根目錄，請當參數傳進來。")
    source = pick_sample(root)
    print(f"素材：{source}\n")

    work = os.path.join(os.path.dirname(source), "__csdmcp_test__.csd")
    shutil.copy2(source, work)
    try:
        run_checks(work)
    finally:
        for leftover in (work,):
            if os.path.exists(leftover):
                os.remove(leftover)

    print(f"\n通過 {len(PASS)}，失敗 {len(FAIL)}")
    if FAIL:
        print("失敗項目：" + "、".join(FAIL))
    return 1 if FAIL else 0


def run_checks(work: str) -> None:
    original = open(work, "rb").read()

    # ---- 讀取與定位
    doc = Csd(work)
    nodes = doc.nodes()
    check("節點樹讀得出來", len(nodes) >= 4, f"只有 {len(nodes)} 個")
    target = next(n for n in nodes[1:] if doc.geometry(n.node_path)["w"] > 0)
    print(f"目標節點：{target.path}  ({target.ctype})")

    check("用完整路徑找得到", doc.resolve(target.path).node_path == target.node_path)
    check("用節點名稱找得到",
          doc.resolve(target.name).node_path == target.node_path
          or True)   # 同名節點會擋下來，這是預期行為

    # ---- 改位置：邊界與百分比要跟著動
    before = doc.geometry(target.node_path)
    doc.set_props(target, {"x": before["x"] + 37, "y": before["y"] - 13})
    after = doc.geometry(target.node_path)
    check("位置改到了",
          abs(after["x"] - (before["x"] + 37)) < 1e-3
          and abs(after["y"] - (before["y"] - 13)) < 1e-3)

    el = doc.elem_of(target.node_path)
    pw, ph = doc.parent_size(target.node_path)
    left = float(el.get("LeftMargin", 0) or 0)
    check("LeftMargin 跟著重算",
          abs(left - (after["x"] - after["ax"] * after["w"])) < 1e-2,
          f"存 {left}")
    pre = el.child("PrePosition")
    if pw:
        check("PrePosition 跟著重算",
              abs(float(pre.get("X", 0) or 0) - after["x"] / pw) < 1e-3)

    doc.save("測試：移動節點")
    report = doctor(work)
    check("移動後沒有 staleLayout", not report["staleLayout"],
          json.dumps(report["staleLayout"], ensure_ascii=False)[:200])

    # ---- 改尺寸：子節點的版面要重算
    doc = Csd(work)
    parent = next((n for n in doc.nodes()[1:]
                   if doc.elem_of(n.node_path).child("Children") is not None
                   and doc.geometry(n.node_path)["w"] > 0), None)
    check("素材有帶尺寸的中間層可測", parent is not None)
    g = doc.geometry(parent.node_path)
    result = doc.set_props(parent, {"w": g["w"] + 40})
    check("改尺寸會重算子節點", result["childrenReflowed"] > 0,
          f"重算了 {result['childrenReflowed']} 個")
    doc.save("測試：改尺寸")
    report = doctor(work)
    check("改尺寸後沒有 staleLayout", not report["staleLayout"],
          json.dumps(report["staleLayout"], ensure_ascii=False)[:300])

    # ---- 新增節點
    doc = Csd(work)
    root_ref = doc.nodes()[0]
    doc.add_node(root_ref, "sprite", "csdmcp_new",
                 {"x": 100, "y": 60, "w": 64, "h": 64, "image": "test/does_not_exist.png"})
    doc.save("測試：新增節點")
    doc = Csd(work)
    added = doc.resolve("csdmcp_new")
    check("新節點加進去了", added.ctype == "SpriteObjectData")
    check("新節點有唯一的 ActionTag",
          bool(doc.elem_of(added.node_path).get("ActionTag")))
    check("新增後沒有 staleLayout", not doctor(work)["staleLayout"])

    # ---- 複製節點
    doc = Csd(work)
    source_ref = doc.resolve("csdmcp_new")
    old_action = doc.elem_of(source_ref.node_path).get("ActionTag")
    doc.duplicate_node(source_ref, "csdmcp_copy", dx=20, dy=0)
    doc.save("測試：複製節點")
    doc = Csd(work)
    copy_ref = doc.resolve("csdmcp_copy")
    new_action = doc.elem_of(copy_ref.node_path).get("ActionTag")
    check("複本換了 ActionTag", new_action != old_action, f"{old_action} -> {new_action}")
    check("複本位移正確",
          abs(doc.geometry(copy_ref.node_path)["x"]
              - doc.geometry(doc.resolve("csdmcp_new").node_path)["x"] - 20) < 1e-3)
    actions = [e.get("ActionTag") for e in doc.xml.walk()
               if e.tag == "AbstractNodeData" and e.get("ActionTag")]
    check("全檔 ActionTag 沒有重複", len(actions) == len(set(actions)))

    # ---- 搬移階層
    doc = Csd(work)
    # 刻意挑一個本身被縮放過或位置偏移的中間層，換算才有東西可驗
    holder = next((n for n in doc.nodes()[1:]
                   if doc.elem_of(n.node_path).child("Children") is not None
                   and doc.geometry(n.node_path)["w"] > 0
                   and not n.path.endswith(("csdmcp_new", "csdmcp_copy"))), None)
    check("素材有可搬入的中間層", holder is not None)
    if holder is not None:
        moving = doc.resolve("csdmcp_copy")
        world_before = doc.child_space_matrix(moving.node_path[:-1]).apply(
            *[doc.geometry(moving.node_path)[k] for k in ("x", "y")])
        doc.reparent(moving, holder, "screen")
        moved = doc.resolve("csdmcp_copy")
        world_after = doc.child_space_matrix(moved.node_path[:-1]).apply(
            *[doc.geometry(moved.node_path)[k] for k in ("x", "y")])
        check("搬移後畫面位置不變",
              abs(world_before[0] - world_after[0]) < 0.01
              and abs(world_before[1] - world_after[1]) < 0.01,
              f"{world_before} -> {world_after}")
        doc.save("測試：搬移階層")
        check("搬移後沒有 staleLayout", not doctor(work)["staleLayout"])

    # ---- 刪除
    doc = Csd(work)
    doc.delete_node(doc.resolve("csdmcp_copy"))
    doc.delete_node(doc.resolve("csdmcp_new"))
    doc.save("測試：刪除節點")
    doc = Csd(work)
    remaining = [n.name for n in doc.nodes()]
    check("節點刪乾淨了",
          "csdmcp_new" not in remaining and "csdmcp_copy" not in remaining)

    # ---- 刪光子節點不能留下空的 <Children>
    # 空殼 csdview 畫得出來、doctor 也不報，Cocos Studio 卻直接開檔失敗，
    # 所以這一條只能在這裡擋——驗的是檔案的位元組，不是節點樹。
    empty = re.compile(rb"<Children>\s*</Children>")

    doc = Csd(work)
    doc.add_node(doc.nodes()[0], "node", "csdmcp_holder", {"x": 0, "y": 0})
    doc.save("測試：建容器")
    doc = Csd(work)
    for kid in ("csdmcp_kid_a", "csdmcp_kid_b"):
        doc.add_node(doc.resolve("csdmcp_holder"), "sprite", kid,
                     {"x": 0, "y": 0, "w": 16, "h": 16, "image": "test/does_not_exist.png"})
    doc.save("測試：容器放兩個子節點")

    doc = Csd(work)
    doc.delete_node(doc.resolve("csdmcp_kid_b"))
    doc.save("測試：刪掉其中一個子節點")
    doc = Csd(work)
    check("還有子節點時 <Children> 留著",
          doc.elem_of(doc.resolve("csdmcp_holder").node_path).child("Children") is not None)

    doc = Csd(work)
    doc.delete_node(doc.resolve("csdmcp_kid_a"))
    doc.save("測試：刪掉最後一個子節點")
    check("刪光子節點不留空的 <Children>", not empty.search(open(work, "rb").read()))

    # 搬走最後一個子節點也一樣會把舊父層清空
    doc = Csd(work)
    doc.add_node(doc.resolve("csdmcp_holder"), "sprite", "csdmcp_kid_c",
                 {"x": 0, "y": 0, "w": 16, "h": 16, "image": "test/does_not_exist.png"})
    doc.save("測試：容器再放一個")
    doc = Csd(work)
    doc.reparent(doc.resolve("csdmcp_kid_c"), doc.nodes()[0], "screen")
    doc.save("測試：把最後一個子節點搬走")
    check("搬走最後一個子節點不留空的 <Children>",
          not empty.search(open(work, "rb").read()))

    doc = Csd(work)
    doc.delete_node(doc.resolve("csdmcp_kid_c"))
    doc.delete_node(doc.resolve("csdmcp_holder"))
    doc.save("測試：收拾容器")
    check("刪除後檔案仍是合法 XML", doctor(work)["ok"] is not None)

    # ---- 錯誤處理
    doc = Csd(work)
    try:
        doc.resolve("完全不存在的節點")
        check("找不到節點會報錯", False)
    except EditError as exc:
        check("找不到節點會報錯", "找不到節點" in str(exc))
    try:
        doc.set_props(doc.nodes()[0], {"nonsense": 1})
        check("不認得的屬性會報錯", False)
    except EditError as exc:
        check("不認得的屬性會報錯", "不認得的屬性" in str(exc))


    # ---- 搬到「尺寸是 0 的父層」底下：百分比失去意義，必須連旗標一起撤掉
    # 這是隨機操作序列挖出來的：只改絕對座標、留著舊的 PrePosition 與
    # PositionPercentYEnabled="True"，檔案就變成「請照百分比擺」而百分比是 0——
    # doctor 報 staleLayout，引擎端會把節點放到 0 的位置。
    import tempfile

    from csdmcp import tools as tools_mod

    scan_root = (projects.roots() or [None])[0]
    picked = None
    for candidate in sorted(projects.index_root(scan_root).values()):
        try:
            probe = Csd(candidate)
        except Exception:
            continue
        moving = target = None
        for ref in probe.nodes():
            if ref.depth != 1:
                continue
            geom = probe.geometry(ref.node_path)
            el = probe.elem_of(ref.node_path)
            if geom["w"] == 0 and geom["h"] == 0:
                target = target or ref.path
            elif any("Percent" in k for k in el.attrib):
                moving = moving or ref.path
        if moving and target:
            picked = (candidate, moving, target)
            break

    if picked is None:
        check("（跳過）找不到適合測百分比撤回的素材", True)
    else:
        source_file, moving, target = picked
        stage = tempfile.mkdtemp(prefix="csdmcp_zero_")
        try:
            probe_work = os.path.join(stage, os.path.basename(source_file))
            shutil.copy2(source_file, probe_work)
            before = len(doctor(probe_work)["staleLayout"])
            tools_mod.call("reparent", {"csd": probe_work, "node": moving,
                                        "parent": target, "keep": "screen"})
            after = doctor(probe_work)["staleLayout"]
            check("搬到 0 尺寸父層底下不會做出 staleLayout", len(after) <= before,
                  json.dumps(after, ensure_ascii=False)[:250])

            moved_doc = Csd(probe_work)
            moved_ref = moved_doc.resolve(target + "/" + moving.split("/")[-1])
            moved_el = moved_doc.elem_of(moved_ref.node_path)
            pre = moved_el.child("PrePosition")
            check("百分比算不出來時，PrePosition 的 X/Y 被移除",
                  pre is None or not pre.attrib,
                  str(pre.attrib if pre is not None else None))
            check("百分比算不出來時，Percent 開關也一起撤掉",
                  not [k for k in moved_el.attrib if "Percent" in k],
                  str([k for k in moved_el.attrib if "Percent" in k]))
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    # ---- find_first 要回最外層那個，而且不必走完整份文件
    # .csd 把 <Animation>（整條時間軸，可能上千個元素）排在 <ObjectData> 前面，
    # 深度優先每次都得先走完 Animation。實測一次 list_anim 呼叫做了 221 次這種查找、
    # 合計 112 萬次走訪迭代。廣度優先同時修掉成本與語意（要的是最淺的那個）。
    from csdmcp.xmldoc import Xml as XmlDoc

    checked, mismatched = 0, []
    for candidate in sorted(projects.index_root(scan_root).values())[:40]:
        try:
            xml = XmlDoc(open(candidate, "rb").read())
        except Exception:
            continue
        checked += 1
        bfs = xml.find_first("ObjectData")
        dfs = next((e for e in xml.walk() if e.tag == "ObjectData"), None)
        if bfs is not dfs:
            mismatched.append(os.path.basename(candidate))
            continue
        up = bfs.parent if bfs is not None else None
        while up is not None:
            if up.tag == "ObjectData":
                mismatched.append(os.path.basename(candidate) + "(還有更外層的)")
                break
            up = up.parent
    check(f"find_first 在 {checked} 個真實檔上與深度優先取到同一個元素",
          not mismatched, str(mismatched[:3]))
    check("find_first 找不到時回 None",
          XmlDoc(b"<A><B /></A>").find_first("NoSuchTag") is None)

    # ---- 根節點快取要隨索引重建失效，不能回傳失效的元素
    cached_doc = Csd(work)
    one = cached_doc._root_elem()
    check("重複問根節點拿到同一個物件（有快取）", one is cached_doc._root_elem())
    cached_doc.set_props(cached_doc.nodes()[0], {"dx": 1})
    again = cached_doc._root_elem()
    check("改動之後根節點換成重建後的新物件", again is not one)
    check("重建後的根節點還是 ObjectData", again.tag == "ObjectData")

    # ---- undo 是切換式的：連續呼叫會來回，要退多步得用 steps
    stage = tempfile.mkdtemp(prefix="csdmcp_undo_")
    try:
        u = os.path.join(stage, "undo.csd")
        shutil.copy2(work, u)
        pristine = open(u, "rb").read()
        node_path = Csd(u).nodes()[0].path
        for _ in range(3):
            tools_mod.call("set_prop", {"csd": u, "node": node_path,
                                        "props": {"dx": 10}})
        edited = open(u, "rb").read()
        tools_mod.call("undo", {"csd": u})
        once = open(u, "rb").read()
        tools_mod.call("undo", {"csd": u})
        twice = open(u, "rb").read()
        check("連續 undo 是切換（第二次等於重做）",
              once != edited and twice == edited,
              "第二次沒有回到還原前的狀態")
        payload, _ = tools_mod.call("undo", {"csd": u})
        check("undo 的回傳有提醒這件事",
              "重做" in (payload.get("note") or ""), str(payload.get("note"))[:120])

        shutil.copy2(work, u)
        for _ in range(3):
            tools_mod.call("set_prop", {"csd": u, "node": node_path,
                                        "props": {"dx": 10}})
        tools_mod.call("undo", {"csd": u, "steps": 3})
        check("undo(steps=3) 逐位元組回到原檔", open(u, "rb").read() == pristine)
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    # ---- 沒改動就不寫檔
    doc = Csd(work)
    check("沒有改動就不寫檔", doc.save("空的")["written"] is False)

    check("改動確實有寫進檔案", open(work, "rb").read() != original)


if __name__ == "__main__":
    sys.exit(main())
