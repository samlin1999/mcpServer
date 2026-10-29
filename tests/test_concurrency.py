"""競態與資料完整性的測試。

驗的都是「不修就會靜默吃掉使用者工作」的路徑：兩個 agent 同時改同一個檔、
使用者在 Cocos Studio 裡按了存檔、兩個行程同時建檔或同時寫快照。
這類問題不會拋例外、不會留下痕跡，所以只能靠測試把它釘住。

用法：python tests/test_concurrency.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def check(name: str, condition, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"[{'  ok  ' if condition else ' FAIL '}] {name}"
          + (f"  — {detail}" if detail and not condition else ""))


def main() -> int:
    from csdmcp import history, layout, projects
    from csdmcp.editor import Csd, EditError, create
    from csdmcp.xmldoc import Xml

    root = projects.roots()[0]
    src = os.path.join(root, "coinMonopoly", "FirstPlatView.csd")
    work = src + ".conc.csd"
    shutil.copy2(src, work)

    try:
        # ---- 遺失更新：後存的必須被擋下來，不能無聲蓋掉
        a, b = Csd(work), Csd(work)
        a.set_props(a.resolve("Layer/Node_Grid"), {"dx": 100})
        a.save("A")
        b.set_props(b.resolve("Layer/Node_Grid"), {"dy": 50})
        try:
            b.save("B")
            check("並行編輯會被擋下", False, "B 蓋掉了 A，沒有報錯")
        except EditError as exc:
            check("並行編輯會被擋下", "被別的程式改過" in str(exc))
            check("錯誤訊息說得出怎麼處理", "重新讀" in str(exc), str(exc)[:120])

        after = Csd(work)
        g = after.geometry(after.resolve("Layer/Node_Grid").node_path)
        check("A 的改動保住了", abs(g["x"] - 350.0) < 0.01, f"x={g['x']}")

        # ---- 外部只是 touch、內容沒變，不該擋
        c = Csd(work)
        os.utime(work, None)
        c.set_props(c.resolve("Layer/Node_Grid"), {"dx": 1})
        try:
            c.save("touch 後仍可存檔")
            check("只被 touch 過不會誤擋", True)
        except EditError as exc:
            check("只被 touch 過不會誤擋", False, str(exc)[:120])

        # ---- 模擬 Cocos Studio 存檔：內容真的變了就要擋
        d = Csd(work)
        with open(work, "ab") as fh:
            fh.write("\r\n<!-- 別的程式寫的 -->".encode("utf-8"))
        d.set_props(d.resolve("Layer/Node_Grid"), {"dx": 1})
        try:
            d.save("應該要失敗")
            check("外部真的改過內容會被擋", False, "沒有報錯")
        except EditError:
            check("外部真的改過內容會被擋", True)
    finally:
        os.remove(work)

    # ---- create 的 TOCTOU：多執行緒同時建同一個檔，只能有一個成功
    stage = tempfile.mkdtemp(prefix="csdmcp_conc_")
    try:
        target = os.path.join(stage, "Race.csd")
        outcome: list = []
        lock = threading.Lock()

        def build():
            try:
                create(target, "node")
                with lock:
                    outcome.append("ok")
            except EditError:
                with lock:
                    outcome.append("blocked")

        threads = [threading.Thread(target=build) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("同時建檔只有一個成功", outcome.count("ok") == 1,
              f"成功 {outcome.count('ok')} 次：{outcome}")

        # ---- 快照序號：多執行緒同時存快照，不能互相覆蓋
        sample = os.path.join(stage, "Snap.csd")
        create(sample, "node")
        errors: list = []

        def snap(index):
            try:
                history.snapshot(sample, f"第 {index} 次")
            except Exception as exc:      # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=snap, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("並行寫快照不會出錯", not errors, "; ".join(errors[:3]))
        entries = history.history(sample)
        serials = [e["serial"] for e in entries]
        check("快照序號沒有重複", len(serials) == len(set(serials)), str(serials))
        check("12 次快照全都記下來了", len(entries) == 12, f"只有 {len(entries)} 筆")
        bucket = os.path.dirname(history._meta_path(sample))
        files = [f for f in os.listdir(bucket) if f.endswith(".csd")]
        check("快照檔數量對得上", len(files) == 12, f"{len(files)} 個檔 vs {len(entries)} 筆紀錄")

        # ---- 紀錄檔是原子寫入：不會出現半截 JSON
        with open(history._meta_path(sample), encoding="utf-8") as fh:
            check("紀錄檔是合法 JSON", isinstance(json.load(fh), list))
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    # ---- 登錄檔並行寫入：csdview setup 與 add_project 共用同一份，不能互蓋
    stage = tempfile.mkdtemp(prefix="csdmcp_reg_")
    old_reg = projects.REGISTRY
    try:
        projects.REGISTRY = os.path.join(stage, "projects.json")
        dirs = []
        for i in range(8):
            d = os.path.join(stage, f"p{i}")
            os.makedirs(os.path.join(d, "cocosstudio"))
            open(os.path.join(d, "game.ccs"), "w").close()
            dirs.append(d)
        errors: list = []

        def add(d):
            try:
                projects.add_project(d)
            except Exception as exc:      # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=add, args=(d,)) for d in dirs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        data = projects.load_registry()
        check("並行 add_project 沒有出錯", not errors, "; ".join(errors[:3]))
        check("8 個專案都寫進登錄檔", len(data) == 8, f"只有 {len(data)} 筆")
        names = {os.path.normcase(e["directory"]) for e in data}
        check("登錄檔沒有漏掉任何一個",
              names == {os.path.normcase(d) for d in dirs})
    finally:
        projects.REGISTRY = old_reg
        shutil.rmtree(stage, ignore_errors=True)

    # ---- undo 不能蓋掉 Cocos Studio／另一個 agent 的存檔
    work2 = src + ".undo.csd"
    shutil.copy2(src, work2)
    try:
        doc = Csd(work2)
        was = doc.geometry(doc.resolve("Layer/Node_Grid").node_path)["x"]
        doc.set_props(doc.resolve("Layer/Node_Grid"), {"dx": 11})
        doc.save("undo 前的改動")
        rec = history.undo(work2, 1)
        check("沒被外部改過時 undo 成功", rec.get("restored", {}).get("serial", 0) >= 1)
        back = Csd(work2)
        g = back.geometry(back.resolve("Layer/Node_Grid").node_path)
        check("undo 真的還原了", abs(g["x"] - was) < 0.01, f"x={g['x']}")

        doc = Csd(work2)
        doc.set_props(doc.resolve("Layer/Node_Grid"), {"dx": 13})
        doc.save("接著又改一次")
        with open(work2, "ab") as fh:
            fh.write("\r\n<!-- 別的程式寫的 -->".encode("utf-8"))
        try:
            history.undo(work2, 1)
            check("undo 會擋外部改動", False, "還原把外部改動蓋掉了")
        except ValueError as exc:
            check("undo 會擋外部改動", "被別的程式改過" in str(exc))
            check("undo 擋下時說得出怎麼處理", "重新讀" in str(exc), str(exc)[:160])
        with open(work2, "rb") as fh:
            check("undo 擋下後原檔沒動", "別的程式".encode("utf-8") in fh.read())
    finally:
        os.remove(work2)

    # ---- 屬性順序：同時補多個屬性不能反序
    x = Xml(b'<A><Position /><CColor B="3" /></A>')
    x.attr_order = layout.ATTR_ORDER
    x.set_attrs(x.root.child("Position").route, {"X": "60.0000", "Y": "-40.0000"})
    x.set_attrs(x.root.child("CColor").route, {"A": "255", "R": "1", "G": "2"})
    text = x.data.decode()
    check("Position 的 X 排在 Y 前面", '<Position X="60.0000" Y="-40.0000" />' in text, text)
    check("CColor 照 A R G B 排序", '<CColor A="255" R="1" G="2" B="3" />' in text, text)

    # ---- 多行文案：字面換行會被 XML 屬性值正規化吃掉，必須寫成字元參照
    import xml.etree.ElementTree as ET
    y = Xml(b"<A><B/></A>")
    y.set_attrs(y.root.child("B").route, {"LabelText": "第一行\n第二行\t有tab"})
    check("換行寫成字元參照", b"&#xA;" in y.data, y.data.decode())
    back = ET.fromstring(y.data.decode("utf-8")).find("B").get("LabelText")
    check("換行與 tab 解析後還在", back == "第一行\n第二行\t有tab", repr(back))

    # ---- 渲染檔名要跨行程穩定
    code = ("import sys; sys.path.insert(0, r'%s');"
            "from csdmcp import view; import json, hashlib;"
            "print(hashlib.sha1(json.dumps(['a.csd', {}], sort_keys=True)"
            ".encode()).hexdigest()[:12])" % ROOT)
    seen = {subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True).stdout.strip() for _ in range(3)}
    check("渲染檔名跨行程一致", len(seen) == 1, str(seen))

    # ---- 換檔時目標正被別人開著讀：要靠退避重試撐過去，不能直接 PermissionError
    # Windows 的 os.replace 遇到「目標被別的行程開著」會失敗（Python 開檔沒帶
    # FILE_SHARE_DELETE）。對手是常態存在的：csdview 的 watch、Cocos Studio、
    # 另一個 agent 都可能正在讀同一個 .csd。
    stage = tempfile.mkdtemp(prefix="csdmcp_repl_")
    try:
        busy = os.path.join(stage, "Busy.csd")
        shutil.copy2(src, busy)

        holding = threading.Event()

        def reader():
            """模擬別人正在讀這個檔：握著 handle 0.15 秒。"""
            with open(busy, "rb") as fh:
                fh.read(16)
                holding.set()
                time.sleep(0.15)

        worker = threading.Thread(target=reader)
        worker.start()
        holding.wait(2)

        doc = Csd(busy)
        was = doc.geometry(doc.resolve("Layer/Node_Grid").node_path)["x"]
        doc.set_props(doc.resolve("Layer/Node_Grid"), {"dx": 7})
        try:
            doc.save("有人開著檔的時候存檔")
            check("目標被開著讀時仍存得進去", True)
        except PermissionError as exc:
            check("目標被開著讀時仍存得進去", False, str(exc))
        worker.join()

        reread = Csd(busy)
        g = reread.geometry(reread.resolve("Layer/Node_Grid").node_path)
        check("撞到之後寫進去的內容是對的", abs(g["x"] - (was + 7)) < 0.01,
              f"x={g['x']}，原本 {was}")

        # 沒有對手的時候不該白白多花退避的時間
        plain = os.path.join(stage, "Plain.csd")
        shutil.copy2(src, plain)
        quiet = Csd(plain)
        quiet.set_props(quiet.resolve("Layer/Node_Grid"), {"dx": 9})
        started = time.monotonic()
        quiet.save("沒有對手")
        spent = time.monotonic() - started
        check("沒撞到時不會多花退避的時間", spent < 1.0, f"花了 {spent:.2f} 秒")

        # ---- 一直被開著：重試撐不過去時要給得出可行動的錯誤，不能是裸的 PermissionError
        stuck = os.path.join(stage, "Stuck.csd")
        shutil.copy2(src, stuck)
        before = io.open(stuck, "rb").read()
        keeper = open(stuck, "rb")          # 從頭到尾不放手
        try:
            doomed = Csd(stuck)
            doomed.set_props(doomed.resolve("Layer/Node_Grid"), {"dx": 3})
            try:
                doomed.save("撐不過去的存檔")
                check("一直被開著時會報錯", False, "居然存成功了")
            except EditError as exc:
                check("一直被開著時會報錯", True)
                check("錯誤說得出要關掉誰", "關掉" in str(exc) and "Cocos Studio" in str(exc),
                      str(exc)[:160])
                check("錯誤說得出原檔沒有動", "原檔沒有動" in str(exc), str(exc)[:160])
            except PermissionError as exc:
                check("一直被開著時會報錯", False, f"漏出了 PermissionError：{exc}")
        finally:
            keeper.close()
        check("報錯之後原檔真的一個位元都沒變",
              io.open(stuck, "rb").read() == before)
        check("報錯之後沒有留下 .tmp",
              not [f for f in os.listdir(stage) if f.endswith(".tmp")],
              str(os.listdir(stage)))
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    print(f"\n通過 {len(PASS)}，失敗 {len(FAIL)}")
    if FAIL:
        print("失敗項目：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
