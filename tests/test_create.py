"""建立新 .csd 與登錄進 .ccs 的測試。

全程在暫存目錄裡做，用 CSDMCP_ROOTS 把那個假專案掛進來，
所以不會動到真正的專案檔——.ccs 是 git 追蹤的檔案，測試不該碰它。

用法：python tests/test_create.py
"""
from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def check(name: str, condition, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"[{'  ok  ' if condition else ' FAIL '}] {name}"
          + (f"  — {detail}" if detail and not condition else ""))


def main() -> int:
    from csdmcp import projects

    real_root = projects.roots()[0]
    from csdmcp import ccs
    real_solution = ccs.find_solution(real_root)
    check("找得到真正的 .ccs", bool(real_solution), str(real_solution))
    if not real_solution:
        return 1

    stage = tempfile.mkdtemp(prefix="csdmcp_create_")
    fake_project = os.path.join(stage, "FakeGame")
    fake_root = os.path.join(fake_project, "cocosstudio")
    os.makedirs(fake_root)
    fake_solution = os.path.join(fake_project, "FakeGame.ccs")
    shutil.copy2(real_solution, fake_solution)

    os.environ["CSDMCP_ROOTS"] = fake_root
    try:
        run_checks(fake_root, fake_solution)
    finally:
        os.environ.pop("CSDMCP_ROOTS", None)
        shutil.rmtree(stage, ignore_errors=True)

    print(f"\n通過 {len(PASS)}，失敗 {len(FAIL)}")
    if FAIL:
        print("失敗項目：" + "、".join(FAIL))
    return 1 if FAIL else 0


def run_checks(root: str, solution: str) -> None:
    from csdmcp import ccs, tools
    from csdmcp.editor import Csd
    from csdmcp.xmldoc import Xml

    before_ccs = open(solution, "rb").read()

    # ---- 建立一個 node 型別的檔案，順便一次建好內容
    made, _ = tools.call("create_csd", {
        "path": "newmod/Csd/Panel.csd", "project": root,
        "rootType": "node",
        "nodes": [
            {"type": "sprite", "name": "bg",
             "props": {"x": 0, "y": 0, "w": 300, "h": 200,
                       "image": "newmod/img/bg.png"}},
            {"type": "text", "name": "title",
             "props": {"x": 0, "y": 80, "w": 200, "h": 30,
                       "text": "標題", "fontSize": 24, "color": "#FFCC00"}},
        ]})
    path = made["file"]
    check("新檔案建出來了", os.path.isfile(path), path)
    check("回報的根節點型別正確", made["rootType"] == "Node", made["rootType"])

    raw = open(path, "rb").read()
    check("用 CRLF 換行", b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""))
    check("沒有 BOM", raw[:3] != b"\xef\xbb\xbf")
    check("沒有 XML 宣告", not raw.lstrip().startswith(b"<?xml"))
    check("結尾沒有多餘換行", raw.endswith(b"</GameFile>"), repr(raw[-14:]))

    Xml(raw)                                   # 還是合法的 XML
    doc = Csd(path)
    names = [n.name for n in doc.nodes()]
    check("兩個節點都建進去了", "bg" in names and "title" in names, str(names))
    check("節點有唯一的 ActionTag",
          len({doc.elem_of(n.node_path).get("ActionTag")
               for n in doc.nodes()[1:]}) == 2)

    # ---- csdview 讀不讀得動
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    done = subprocess.run([sys.executable, "-m", "csdview", "info", path, "--json"],
                          capture_output=True, env=env)
    check("csdview 讀得動新檔案", done.returncode == 0,
          done.stderr.decode("utf-8", "replace")[-300:])
    if done.returncode == 0:
        info = json.loads(done.stdout.decode("utf-8"))
        # csdview 回報型別時會去掉 ObjectData 字尾
        check("csdview 認得節點型別",
              set(info["nodeTypes"]) >= {"Sprite", "Text"},
              str(info.get("nodeTypes")))

    # ---- .ccs 登錄
    registered = made.get("registered")
    check("有登錄進 .ccs", isinstance(registered, dict) and registered.get("added"),
          json.dumps(registered, ensure_ascii=False)[:200])

    after_ccs = open(solution, "rb").read()
    a = before_ccs.decode("utf-8").splitlines()
    b = after_ccs.decode("utf-8").splitlines()
    added = [l[1:] for l in difflib.unified_diff(a, b, lineterm="", n=0)
             if l.startswith("+") and not l.startswith("+++")]
    # 兩層新資料夾各要開閉標籤，加上一行 Project，剛好五行；不該再多任何東西
    check(".ccs 只多了新資料夾與檔案那幾行", len(added) == 5,
          f"多了 {len(added)} 行：{added}")
    check(".ccs 多出來的每一行都是資料夾或這個檔案",
          all(("newmod" in l or "Csd" in l or "Panel.csd" in l or l.strip() == "</Folder>")
              for l in added), str(added))
    check(".ccs 沒有刪掉任何東西",
          not [l for l in difflib.unified_diff(a, b, lineterm="", n=0)
               if l.startswith("-") and not l.startswith("---")])
    check("登錄的項目寫對了",
          '<Project Name="Panel.csd" Type="Node" />' in after_ccs.decode("utf-8"))
    check(".ccs 還是合法 XML", bool(Xml(after_ccs)))

    # ---- 重複登錄不該產生第二筆
    again = ccs.register(solution, root, path, "Node")
    check("重複登錄不會重複寫入", again.get("added") is False, json.dumps(again))
    check("重複登錄後 .ccs 沒變", open(solution, "rb").read() == after_ccs)

    # ---- 取消登錄要回到原狀
    ccs.unregister(solution, root, path)
    reverted = open(solution, "rb").read().decode("utf-8")
    check("取消登錄後項目不見了",
          '<Project Name="Panel.csd" Type="Node" />' not in reverted)
    ccs.register(solution, root, path, "Node")

    # ---- .ccs 一直被開著：登錄要給得出可行動的錯誤，不能漏出 PermissionError
    # 這是最常見的情境：使用者就是開著 Cocos Studio 在看那個專案。
    before_stuck = open(solution, "rb").read()
    keeper = open(solution, "rb")
    try:
        ccs.unregister(solution, root, path)
        check("被開著時 unregister 會報錯", False, "居然寫成功了")
    except ccs.CcsError as exc:
        check("被開著時 unregister 會報錯", True)
        check("錯誤說得出要關掉誰", "關掉" in str(exc), str(exc)[:200])
        check("錯誤說得出 .ccs 沒有動", ".ccs 沒有動" in str(exc), str(exc)[:200])
    except PermissionError as exc:
        check("被開著時 unregister 會報錯", False, f"漏出了 PermissionError：{exc}")
    finally:
        keeper.close()
    check("報錯之後 .ccs 一個位元都沒變", open(solution, "rb").read() == before_stuck)
    check("報錯之後 .ccs 旁邊沒留下 .tmp",
          not [f for f in os.listdir(os.path.dirname(solution)) if f.endswith(".tmp")],
          str(os.listdir(os.path.dirname(solution))))

    # ---- layer 型別與設計解析度
    layer, _ = tools.call("create_csd", {
        "path": "newmod/Csd/Screen.csd", "project": root,
        "rootType": "layer", "width": 1280, "height": 720})
    check("layer 型別建得出來", layer["rootType"] == "Layer")
    done = subprocess.run(
        [sys.executable, "-m", "csdview", "info", layer["file"], "--json"],
        capture_output=True, env=env)
    if done.returncode == 0:
        info = json.loads(done.stdout.decode("utf-8"))
        check("設計解析度寫對了", info["canvasSize"] == [1280.0, 720.0],
              str(info["canvasSize"]))
        check("csdview 認得根節點型別", info["rootType"] == "Layer", info["rootType"])

    # ---- 同名檔案要擋下來
    from csdmcp.editor import EditError
    try:
        tools.call("create_csd", {"path": "newmod/Csd/Screen.csd", "project": root})
        check("同名檔案沒 overwrite 會被擋", False, "沒有丟例外")
    except EditError as exc:
        check("同名檔案沒 overwrite 會被擋", "已經存在" in str(exc), str(exc))


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        import traceback
        traceback.print_exc()
        code = 1
    sys.exit(code)
