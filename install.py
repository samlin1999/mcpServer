"""安裝：檢查環境並把這個 MCP 伺服器註冊到 Claude Code 與 Cursor。

為什麼安裝流程寫在 Python 而不是 .bat：cmd.exe 是一邊執行一邊按**位元組偏移**
重讀批次檔的，檔案裡有中文、又在中途 `chcp` 切編碼的話，解析位置會整個錯位，
接下來的 echo 文字就被當成指令去執行（畫面上是一串「不是內部或外部命令」）。
所以 .bat 只留純 ASCII 的三行，訊息一律由這裡印。

python.exe / run.py 的路徑由這支程式自己找，不要寫死、也不要叫使用者填
`C:\\Path\\to\\python.exe`。Cursor 的 mcp.json 必須是絕對路徑（它啟動子行程時
工作目錄不一定在這裡），所以搜到之後再展開成絕對路徑寫進去。

用法：安裝.bat（或直接 python install.py）
      python install.py --show-config   # 只印出填好路徑的設定，不寫檔
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def use_utf8_console() -> None:
    """把主控台與自己的輸出都切到 UTF-8。

    切編碼這件事一定要在 Python 行程裡做，不能寫在 .bat 裡：cmd.exe 是一邊執行
    一邊按位元組偏移重讀批次檔的，中途 `chcp` 會讓它的解析位置錯位。
    在這裡切則只影響已經啟動的主控台，批次檔的解析早就結束了。

    兩件事都要做：SetConsoleOutputCP 決定主控台怎麼「解讀」收到的位元組，
    reconfigure 決定 Python 怎麼「產生」那些位元組，只做一半照樣是亂碼。
    """
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def say(text: str = "") -> None:
    print(text)


def run(command: list, **kwargs):
    return subprocess.run(command, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kwargs)


def find_python() -> str:
    """找出要用來跑 MCP 的 python.exe。

    優先用「正在執行這支安裝程式」的解譯器——使用者打 `python install.py`
    就是這個。再從 PATH 找 `python` / `python3`，Windows 另外問 `py -3`。
    若有多個，挑第一個能 `import csdview` 的，這樣 Cursor 連上後檢視工具才跑得動。
    """
    candidates: list[str] = []

    def add(path: str | None) -> None:
        if not path:
            return
        full = os.path.abspath(path)
        if not os.path.isfile(full):
            return
        key = os.path.normcase(full)
        if any(os.path.normcase(item) == key for item in candidates):
            return
        candidates.append(full)

    add(sys.executable)
    for name in ("python", "python3"):
        add(shutil.which(name))
    if os.name == "nt":
        launcher = shutil.which("py") or shutil.which("py.exe")
        if launcher:
            probe = run([launcher, "-3", "-c", "import sys; print(sys.executable)"])
            if probe.returncode == 0:
                add(probe.stdout.strip())

    if not candidates:
        raise SystemExit(
            "找不到 Python。請安裝 Python 3.9+，安裝時勾選 Add Python to PATH，"
            "或用「py -3 install.py」再跑一次。")

    for path in candidates:
        probe = run([path, "-c", "import csdview"])
        if probe.returncode == 0:
            return path
    return candidates[0]


def find_claude() -> "str | None":
    """找 claude 指令。

    Windows 上它是 claude.cmd，而 subprocess 不透過 shell 時不會自己補副檔名，
    所以要把找到的完整路徑拿來用。
    """
    for name in ("claude.cmd", "claude.exe", "claude"):
        found = shutil.which(name)
        if found:
            return found
    return None


def cursor_mcp_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".cursor", "mcp.json")


def cursor_server_entry(python: str, launcher: str) -> dict:
    """Cursor 的 stdio MCP 設定。

    command / args 一律用絕對路徑：Cursor 啟動子行程時的工作目錄
    不一定是這個資料夾。-u 與 PYTHONUNBUFFERED 避免 stdout 被緩衝，
    協定通道卡住時客戶端只會顯示連線中。
    """
    return {
        "type": "stdio",
        "command": python,
        "args": ["-u", launcher],
        "env": {
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
        },
    }


def register_cursor(python: str, launcher: str) -> "tuple[bool, str]":
    """把 csd 寫進 ~/.cursor/mcp.json，既有的其他伺服器原樣保留。"""
    path = cursor_mcp_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)

    config: dict = {"mcpServers": {}}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                raw = fh.read()
            if raw.strip():
                loaded = json.loads(raw)
                if not isinstance(loaded, dict):
                    return False, f"{path} 根節點不是物件，沒有改寫以免弄壞現有設定。"
                config = loaded
        except ValueError as exc:
            return False, f"無法解析現有的 {path}：{exc}"

    servers = config.get("mcpServers")
    if servers is None:
        servers = {}
        config["mcpServers"] = servers
    if not isinstance(servers, dict):
        return False, f"{path} 的 mcpServers 不是物件，沒有改寫以免弄壞現有設定。"

    servers["csd"] = cursor_server_entry(python, launcher)

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(config, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    return True, path


def _parse_args(argv: "list | None") -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="檢查環境並把 csd MCP 註冊到 Claude Code 與 Cursor。"
                    "python.exe 與 run.py 會自動搜尋，不必手填路徑。")
    parser.add_argument(
        "--show-config", action="store_true",
        help="只印出填好本機路徑的 Cursor mcp.json 片段，不寫檔、不註冊。")
    return parser.parse_args(argv)


def show_config(python: str, launcher: str) -> None:
    snippet = {"mcpServers": {"csd": cursor_server_entry(python, launcher)}}
    say(json.dumps(snippet, indent=2, ensure_ascii=False))
    say()
    say(f"  Python : {python}")
    say(f"  啟動檔 : {launcher}")
    say(f"  寫入位置: {cursor_mcp_path()}")
    say("  （這段是搜出來的本機路徑，直接跑「安裝.bat」會寫進去，不必手貼。）")


def main(argv: "list | None" = None) -> int:
    use_utf8_console()
    args = _parse_args(argv)
    python = find_python()
    launcher = os.path.join(HERE, "run.py")

    if args.show_config:
        show_config(python, launcher)
        return 0

    say()
    say("  csdmcp — 安裝 .csd 編輯 MCP 伺服器")
    say("  " + "=" * 36)
    say()
    say(f"  使用的 Python：{python}")
    say()

    # ---- 1. csdview
    say("  [1/3] 檢查 csdview ...")
    probe = run([python, "-c",
                 "import csdview, os; print(os.path.dirname(csdview.__file__))"])
    if probe.returncode != 0:
        say()
        say("  [x] 這個 Python 環境裡沒有 csdview。")
        say("      請先到 CsdAiViewer 資料夾執行： pip install -e .")
        say()
        say(f"      目前使用的 Python：{python}")
        return 1
    say(f"        找到 {probe.stdout.strip()}")

    # ---- 2. 自我檢查
    say("  [2/3] 自我檢查 ...")
    check = run([python, os.path.join(HERE, "tests", "test_protocol.py")],
                cwd=HERE, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    if check.returncode == 0:
        say("        通過")
    else:
        say("  [!] 自我檢查沒過。仍然會繼續安裝，但建議先看看哪裡有問題：")
        say(f"        {python} tests\\test_protocol.py")
        tail = [line for line in (check.stdout or "").splitlines() if "FAIL" in line]
        for line in tail[:5]:
            say(f"        {line.strip()}")

    # ---- 3. 註冊（Claude Code 與 Cursor 各自獨立，缺一個不算整次失敗）
    say("  [3/3] 註冊 MCP 客戶端 ...")
    registered = []

    say("        Claude Code")
    claude = find_claude()
    if not claude:
        say("          [!] 找不到 claude 指令，略過。要給 Claude Code 用請先安裝它。")
    else:
        run([claude, "mcp", "remove", "--scope", "user", "csd"])
        added = run([claude, "mcp", "add", "--scope", "user", "csd",
                     "--", python, launcher])
        if added.returncode != 0:
            say("          [x] 註冊失敗：")
            say("              " + (added.stderr or added.stdout or "").strip()[:500])
        else:
            registered.append("claude")
            say("          已註冊（使用者層級，所有專案都用得到）")
            listing = run([claude, "mcp", "list"])
            for line in (listing.stdout or "").splitlines():
                if line.strip().startswith("csd:"):
                    say(f"          狀態： {line.strip()}")
                    break

    say("        Cursor")
    ok, detail = register_cursor(python, launcher)
    if not ok:
        say("          [x] 註冊失敗：")
        say(f"              {detail}")
    else:
        registered.append("cursor")
        say(f"          已寫入 {detail}")
        say("          （使用者層級，所有 Cursor 專案都用得到；既有的其他 MCP 伺服器會保留）")

    if not registered:
        say()
        say("  [x] Claude Code 與 Cursor 都沒註冊成功。")
        return 1

    say()
    say("  完成。")
    if "claude" in registered:
        say("  · Claude Code：開一個新的對話才會載入這些工具。")
    if "cursor" in registered:
        say("  · Cursor：打開 Settings → MCP，確認 csd 已連線；沒出現就重開 Cursor。")
    say()
    say("  試試看：跟助理說「把某個 csd 的某個節點往右移 20」。")
    say("  認得的專案沿用 csdview 的設定；要加新專案就說「登錄一個新的 Cocos Studio 專案」。")
    say()
    return 0


if __name__ == "__main__":
    sys.exit(main())
