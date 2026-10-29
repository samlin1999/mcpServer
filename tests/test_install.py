"""安裝程式會自己找 python.exe，不要叫使用者填絕對路徑。

用法：python tests/test_install.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import install                                    # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"[{'  ok  ' if condition else ' FAIL '}] {name}"
          + (f"  — {detail}" if detail and not condition else ""))


def main() -> int:
    python = install.find_python()
    launcher = os.path.join(install.HERE, "run.py")
    check("find_python 找到現存的解譯器", os.path.isfile(python), python)
    check("run.py 在安裝程式旁邊", os.path.isfile(launcher), launcher)

    entry = install.cursor_server_entry(python, launcher)
    check("Cursor command 就是搜到的 python", entry["command"] == python)
    check("Cursor args 指向本機 run.py", entry["args"] == ["-u", launcher])
    check("沒有寫死的 Path\\to 佔位字串",
          "Path\\to" not in json.dumps(entry) and "Path/to" not in json.dumps(entry))

    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    shown = subprocess.run(
        [sys.executable, os.path.join(ROOT, "install.py"), "--show-config"],
        capture_output=True, text=True, encoding="utf-8", env=env, cwd=ROOT)
    check("--show-config 結束碼 0", shown.returncode == 0, shown.stderr[-400:])
    check("--show-config 印出搜到的 python", python in (shown.stdout or ""),
          shown.stdout[:400])
    check("--show-config 印出 run.py", launcher in (shown.stdout or ""))
    check("--show-config 沒有佔位路徑", "Path\\to" not in (shown.stdout or ""))

    print(f"\n通過 {len(PASS)}，失敗 {len(FAIL)}")
    if FAIL:
        print("失敗項目：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
