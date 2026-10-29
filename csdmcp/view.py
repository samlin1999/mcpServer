"""檢視功能一律轉呼叫 csdview 的 CLI。

刻意不重寫一套渲染或比對：csdview 的行為（判讀準則、verdict 門檻、hints）
已經寫在各專案的 CLAUDE.md 裡，這裡自己算一份遲早會跟文件對不起來。
轉呼叫就永遠一致，csdview 更新也自動跟上。

MCP 比 CLI 多的一件事在這裡：render 出來的圖直接以 base64 回傳，
呼叫端不必先寫檔再讀檔。原尺寸的 PNG 還是會落地，路徑一併回報。
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading

# 回傳給模型看的圖片邊長上限。太大只是浪費 token，看版面對不對用不到原尺寸；
# 需要逐像素檢查時再去讀 JSON 裡回報的原圖路徑。
INLINE_MAX_EDGE = 1400
INLINE_MAX_BYTES = 3_500_000

# 暫存渲染圖保留幾張
RENDER_KEEP = 40
_IN_PROCESS = threading.Lock()


class ViewError(Exception):
    pass


def opt(flag: str, value) -> str:
    """組成 `--flag=value` 的單一參數。

    不能寫成分開的兩個參數：節點名稱、動畫名稱這些值是資料，開頭真的可能是 '-'
    （專案裡就有叫 `-------NoData--------` 的節點），分開傳會被 argparse 當成旗標。
    """
    return f"{flag}={value}"


def _in_process(argv: list):
    """在自己的行程裡跑 csdview，回傳 (離開碼, stdout, stderr)。

    直接呼叫 `csdview.cli.main(argv)`，跟開子行程走的是同一條程式碼，行為零差異，
    但省下每次約 240 ms 的固定成本（python 啟動 52 ms、import csdview 186 ms）——
    那佔了 `info` 呼叫的八成以上。

    stdout 一定要先換掉：這個行程的 stdout 是 MCP 的協定通道，
    csdview 會往那裡印 JSON，混進去就把協定弄壞了。
    （順帶一提，cli.main 開頭會對 stdout 呼叫 reconfigure；StringIO 沒有這個方法，
    它自己的 try/except 會接住，不影響。）

    **前提：呼叫端是單執行緒**。redirect_stdout 換掉的是 sys.stdout，那是行程全域的，
    不是這個呼叫的區域狀態。目前 server.main 是一條循序的訊息迴圈，同一時間只會有
    一個 _in_process 在跑。下面這把鎖是第二道防線：即使將來有人從別的執行緒呼叫
    run()，兩個 _in_process 也不會同時改 sys.stdout。這把鎖擋不住「別的執行緒直接
    print 到 stdout」——那種情況還是得改成 csdview 的函式介面，或走 _subprocess。
    """
    from csdview import cli

    out, err = io.StringIO(), io.StringIO()
    with _IN_PROCESS:
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(argv)
        except SystemExit as exc:            # argparse 參數錯誤時會走這裡
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()


def _subprocess(argv: list):
    """備援：用子行程跑。行程內執行失敗時才會走到這裡。"""
    env = dict(os.environ)
    # Windows 的預設 stdout 編碼是 cp950，中文路徑與文案會在管線裡爆掉
    env["PYTHONIOENCODING"] = "utf-8"
    command = [sys.executable, "-m", "csdview"] + argv
    try:
        done = subprocess.run(command, capture_output=True, env=env, timeout=300)
    except FileNotFoundError as exc:
        raise ViewError(f"叫不到 csdview：{exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ViewError(f"csdview 執行超過 300 秒沒有結束：{' '.join(argv)}") from exc
    return (done.returncode, done.stdout.decode("utf-8", "replace"),
            done.stderr.decode("utf-8", "replace"))


def run(args: list, expect_json: bool = True):
    """執行一次 csdview，回傳解析後的 JSON。"""
    argv = [str(a) for a in args]
    if expect_json and "--json" not in argv:
        argv.append("--json")

    try:
        code, out, err = _in_process(argv)
    except ImportError as exc:
        raise ViewError(
            "這個 Python 環境裡沒有 csdview。請到 CsdAiViewer 資料夾執行 "
            "`pip install -e .`。\n" + str(exc)) from exc
    except Exception:
        # 行程內執行踩到意外狀況（例如 csdview 換了介面）就退回子行程，
        # 至少功能還在，不會整個工具掛掉
        code, out, err = _subprocess(argv)

    # compare 的離開碼 1 代表「差異大」，是正常結果不是錯誤；2 才是執行失敗
    if code not in (0, 1):
        raise ViewError(f"csdview 失敗（離開碼 {code}）：\n{(err or out).strip()}")

    if not expect_json:
        return out
    try:
        return json.loads(out)
    except ValueError as exc:
        raise ViewError(f"csdview 的輸出不是 JSON：{exc}\n{out[:800]}") from exc


def available() -> dict:
    """確認 csdview 這條路走不走得通，開機時檢查用。"""
    try:
        import csdview
        return {"ok": True, "module": os.path.dirname(csdview.__file__),
                "python": sys.executable}
    except ImportError as exc:
        return {"ok": False, "error": str(exc), "python": sys.executable}


def _out_path(tag: str) -> str:
    folder = os.path.join(tempfile.gettempdir(), "csdmcp_render")
    os.makedirs(folder, exist_ok=True)
    _sweep(folder)
    return os.path.join(folder, tag)


def _sweep(folder: str, keep: int = RENDER_KEEP) -> None:
    """清掉舊的渲染輸出，只留最近的幾張。

    沒有這一步的話這個資料夾只會一直長大——同一個檔案換個參數就是一個新檔名，
    沒有任何東西會回頭刪掉它們。
    """
    try:
        files = [os.path.join(folder, f) for f in os.listdir(folder)
                 if f.lower().endswith(".png")]
    except OSError:
        return
    if len(files) <= keep:
        return
    try:
        files.sort(key=os.path.getmtime, reverse=True)
    except OSError:
        return
    for stale in files[keep:]:
        try:
            os.remove(stale)
        except OSError:
            pass          # 別的行程正在讀它，下次再清就好


def render(csd: str, options: dict) -> tuple:
    """渲染一張圖，回傳 (csdview 的 JSON, 內嵌用的 base64 PNG)。"""
    name = os.path.splitext(os.path.basename(csd))[0]
    # 用 sha1 而不是內建的 hash()：字串的 hash 每個行程都不一樣（PYTHONHASHSEED 隨機化），
    # 同樣的請求在不同行程會寫到不同檔名，暫存檔只增不減、也完全沒有重用。
    key = json.dumps([csd, options], sort_keys=True, default=str, ensure_ascii=False)
    stamp = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    out = _out_path(f"{name}_{stamp}.png")

    args = ["render", csd, "-o", out]
    args += frame_args(options)
    if options.get("scale"):
        args.append(opt("--scale", options["scale"]))
    if options.get("annotate"):
        args.append("--annotate")
    if options.get("bg"):
        args.append(opt("--bg", options["bg"]))
    for path in options.get("hide") or []:
        args.append(opt("--hide", path))
    for path in options.get("show") or []:
        args.append(opt("--show", path))
    if options.get("noParticles"):
        args.append("--no-particles")
    if options.get("designOnly"):
        args.append("--no-full-bounds")
    for root in options.get("extraRoots") or []:
        args.append(opt("--extra-root", root))
    if options.get("root"):
        args.append(opt("--root", options["root"]))

    meta = run(args)
    return meta, inline_png(meta.get("image") or out)


def compare(csd: str, reference: str, options: dict) -> dict:
    """跟一張參考圖比對，回傳 csdview 的 JSON。

    一定要帶 `-o`：沒給輸出路徑時 csdview 會把三格對照圖寫成
    `out/<名稱>_diff.png`，那是**相對於行程的工作目錄**——MCP 伺服器的工作目錄
    是使用者的 Creator 專案，等於每次比對都在人家的 repo 裡長出一個 out/，
    直接冒在 git status 上。跟 render 一樣導到系統暫存區。
    """
    name = os.path.splitext(os.path.basename(csd))[0]
    key = json.dumps([csd, reference, options], sort_keys=True, default=str,
                     ensure_ascii=False)
    stamp = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    out = _out_path(f"{name}_{stamp}_diff.png")

    args = ["compare", csd, reference, "-o", out] + frame_args(options)
    if options.get("diffBg"):
        args.append(opt("--diff-bg", options["diffBg"]))
    return run(args)


def frame_args(options: dict) -> list:
    """把 anim / frame / time 換成 csdview 的參數。"""
    args: list = []
    if options.get("anim"):
        args.append(opt("--anim", options["anim"]))
    if options.get("time") is not None:
        args.append(opt("--time", options["time"]))
    elif options.get("frame") is not None:
        args.append(opt("--frame", options["frame"]))
    return args


def inline_png(path: str) -> "dict | None":
    """把 PNG 讀成可內嵌的 base64，太大就先縮圖。"""
    if not path or not os.path.isfile(path):
        return None
    from PIL import Image

    with open(path, "rb") as fh:
        raw = fh.read()
    image = Image.open(io.BytesIO(raw))
    width, height = image.size
    longest = max(width, height)

    if longest > INLINE_MAX_EDGE or len(raw) > INLINE_MAX_BYTES:
        ratio = min(INLINE_MAX_EDGE / longest, 1.0)
        small = image.convert("RGBA").resize(
            (max(1, int(width * ratio)), max(1, int(height * ratio))), Image.LANCZOS)
        buffer = io.BytesIO()
        small.save(buffer, "PNG", optimize=True)
        raw = buffer.getvalue()
        shown = small.size
    else:
        shown = (width, height)

    return {"data": base64.b64encode(raw).decode("ascii"),
            "shownSize": list(shown), "fullSize": [width, height],
            "downscaled": list(shown) != [width, height]}
