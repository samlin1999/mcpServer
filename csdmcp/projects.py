"""要編輯哪個專案的哪個 .csd——路徑解析全部集中在這裡。

刻意不寫死任何專案路徑。來源優先序：

1. 呼叫時直接給的絕對路徑
2. 呼叫時給的 `project`（登錄檔裡的專案名稱或資料夾）
3. 環境變數 `CSDMCP_ROOTS`（用 `os.pathsep` 分隔的額外 cocosstudio 根目錄）
4. csdview 自己的登錄檔 `~/.csdview_projects`——`csdview setup` / `csdview init`
   寫的就是這一份，所以在 csdview 設定過的專案，這裡自動就認得
5. 當前工作目錄往上找 cocosstudio

同一個檔名可能存在於多個專案，這種情況一律回報所有候選並要求指定，
不猜——猜錯會改到另一個專案的檔案。
"""
from __future__ import annotations

import json
import os
import tempfile
import time

from .locking import LockTimeout, file_lock, replace_with_retry

# 兩邊必須算出同一個路徑，否則登錄檔會分家。
# CSDVIEW_PROJECTS 是給測試與 CI 用的：沒有它就只能動使用者真正的那一份，
# 每跑一次測試都會污染它（實測發生過兩次）。
REGISTRY = (os.environ.get("CSDVIEW_PROJECTS")
            or os.path.join(os.path.expanduser("~"), ".csdview_projects"))
_ENV_ROOTS = "CSDMCP_ROOTS"

# 掃描時要跳過的資料夾，跟 csdview 一致
SKIP = {".git", ".svn", "node_modules", "__pycache__", "build", "temp", "Temp",
        ".vs", ".idea", "obj", "bin"}


class ProjectError(Exception):
    """路徑解析失敗；訊息會直接回給呼叫端，所以要寫得能照著解決。"""


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def load_registry() -> list:
    try:
        with open(REGISTRY, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return [item for item in data if isinstance(item, dict) and item.get("directory")]


def _write_registry(entries: list) -> None:
    """原子寫入登錄檔。呼叫端必須已經持有 `file_lock(REGISTRY)`。

    直接 open+write 的話，寫到一半被中斷就留下半截 JSON；csdview `setup`
    與這個工具又會同時改同一份檔，沒有鎖也沒有原子換檔就會互相蓋掉。
    鎖檔路徑是 `~/.csdview_projects.lock`，跟 csdview 約定好的。
    """
    folder = os.path.dirname(REGISTRY) or "."
    handle, temp = tempfile.mkstemp(dir=folder, suffix=".json.tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(entries, fh, ensure_ascii=False, indent=2)
        replace_with_retry(temp, REGISTRY)
    except BaseException:
        if os.path.exists(temp):
            os.remove(temp)
        raise


def _looks_like_root(directory: str) -> bool:
    if os.path.basename(directory).lower() == "cocosstudio":
        return True
    try:
        return any(n.lower().endswith(".ccs") for n in os.listdir(directory))
    except OSError:
        return False


def detect_root(start: str, depth: int = 4) -> "str | None":
    """從某個位置往上找 cocosstudio 根目錄。

    往上找而不是往下掃：已經拿到一個 .csd 的路徑時，根目錄一定在它的祖先裡，
    往下掃一個大專案要好幾秒。
    """
    current = os.path.abspath(start)
    if os.path.isfile(current):
        current = os.path.dirname(current)
    for _ in range(depth + 12):
        if _looks_like_root(current):
            nested = os.path.join(current, "cocosstudio")
            return nested if os.path.isdir(nested) else current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
    return None


def known_projects() -> list:
    """列出所有認得的專案，附上 cocosstudio 根目錄是否還在。"""
    out: list = []
    seen = set()

    def add(name, directory, root, source):
        key = _norm(root) if root else _norm(directory)
        if key in seen:
            return
        seen.add(key)
        out.append({
            "name": name,
            "directory": directory,
            "csdRoot": root,
            "source": source,
            "available": bool(root) and os.path.isdir(root),
        })

    for raw in (os.environ.get(_ENV_ROOTS) or "").split(os.pathsep):
        raw = raw.strip()
        if raw:
            add(os.path.basename(raw.rstrip("\\/")) or raw, raw, raw, _ENV_ROOTS)

    for entry in load_registry():
        directory = entry["directory"]
        root = entry.get("csdRoot") or detect_root(directory)
        add(os.path.basename(directory.rstrip("\\/")), directory, root, "csdview 登錄檔")

    here = detect_root(os.getcwd())
    if here:
        add(os.path.basename(os.path.dirname(here.rstrip("\\/"))) or "cwd",
            os.getcwd(), here, "工作目錄")
    return out


def roots(project: "str | None" = None) -> list:
    """要搜尋的 cocosstudio 根目錄清單。

    指定 project 時只回傳那一個，避免不小心跨專案比對到同名檔案。
    """
    entries = [p for p in known_projects() if p["available"]]
    if project:
        needle = project.strip().lower()
        picked = [p for p in entries
                  if p["name"].lower() == needle
                  or _norm(p["directory"]) == _norm(project)
                  or _norm(p["csdRoot"]) == _norm(project)]
        if not picked and os.path.isdir(project):
            # 直接給一個沒登錄過的資料夾也接受
            root = detect_root(project) or os.path.abspath(project)
            return [root]
        if not picked:
            names = "、".join(p["name"] for p in entries) or "（沒有任何已登錄專案）"
            raise ProjectError(
                f"找不到專案 '{project}'。已知的專案：{names}。"
                f"用 list_projects 看完整清單，或用 add_project 新增一個。")
        return [p["csdRoot"] for p in picked]
    return [p["csdRoot"] for p in entries]


# 目錄索引的快取。走一趟 1500 個 .csd 的專案要 137 ms，而 list_csd 與
# resolve_csd 的退路都會用到；同一次對話裡通常是連續問好幾次，重走沒有意義。
# 有效期短是刻意的：檔案可能被別的程式新增，寧可偶爾多走一趟也不要拿到過期清單。
_INDEX_TTL = 20.0
_index_cache: dict = {}


def index_root(root: str, fresh: bool = False) -> dict:
    """把一個根目錄底下所有 .csd 的相對路徑收成索引。"""
    key = _norm(root)
    if not fresh:
        cached = _index_cache.get(key)
        if cached and time.monotonic() - cached[0] < _INDEX_TTL:
            return cached[1]

    found: dict = {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith(".")]
        for name in files:
            if name.lower().endswith(".csd"):
                full = os.path.join(dirpath, name)
                found[os.path.relpath(full, root).replace("\\", "/")] = full
    _index_cache[key] = (time.monotonic(), found)
    return found


def forget_index(root: "str | None" = None) -> None:
    """建了新檔之後叫一下，讓下一次查詢看得到它。"""
    if root is None:
        _index_cache.clear()
    else:
        _index_cache.pop(_norm(root), None)


def resolve_csd(spec: str, project: "str | None" = None) -> str:
    """把使用者給的東西變成一個確定存在的 .csd 絕對路徑。

    spec 可以是絕對路徑、相對於 cocosstudio 根的路徑、或只有檔名。
    """
    if not spec or not str(spec).strip():
        raise ProjectError("沒有指定 .csd。")
    spec = str(spec).strip().replace("\\", "/")

    if os.path.isabs(spec):
        if os.path.isfile(spec):
            return os.path.abspath(spec)
        raise ProjectError(f"檔案不存在：{spec}")

    candidates: list = []
    for root in roots(project):
        direct = os.path.join(root, spec)
        if os.path.isfile(direct):
            candidates.append(os.path.abspath(direct))

    if not candidates:
        # 退而求其次：只給檔名或尾段路徑時，在索引裡找結尾相符的
        needle = spec.lower()
        if not needle.endswith(".csd"):
            needle += ".csd"
        for root in roots(project):
            for rel, full in index_root(root).items():
                low = rel.lower()
                if low == needle or low.endswith("/" + needle):
                    candidates.append(full)

    unique = list(dict.fromkeys(candidates))
    if not unique:
        scope = f"專案 '{project}'" if project else "所有已知專案"
        raise ProjectError(
            f"在 {scope} 裡找不到 '{spec}'。"
            f"可以用 list_csd 列出可用的檔案，或直接給絕對路徑。")
    if len(unique) > 1:
        listed = "\n".join(f"  {p}" for p in unique[:10])
        raise ProjectError(
            f"'{spec}' 對應到 {len(unique)} 個檔案，請指定 project 或改用絕對路徑：\n{listed}")
    return unique[0]


def resolve_new(spec: str, project: "str | None" = None) -> tuple:
    """解析一個**還不存在**的 .csd 要建在哪，回傳 (絕對路徑, 所屬的 cocosstudio 根)。

    跟 resolve_csd 分開寫：那邊靠「檔案存在」來消歧義，這裡不能。
    相對路徑在多個專案下都成立時一律要求指定 project——建錯地方比找不到麻煩得多。
    """
    if not spec or not str(spec).strip():
        raise ProjectError("沒有指定要建立的 .csd 路徑。")
    spec = str(spec).strip().replace("\\", "/")
    if not spec.lower().endswith(".csd"):
        spec += ".csd"

    available = roots(project)
    if os.path.isabs(spec):
        full = os.path.abspath(spec)
        owner = next((r for r in available
                      if _norm(full).startswith(_norm(r) + os.sep)), None)
        return full, owner or detect_root(full)

    if not available:
        raise ProjectError("沒有任何已知專案，請先用 add_project 登錄，或改給絕對路徑。")
    if len(available) > 1:
        listed = "、".join(os.path.basename(os.path.dirname(r)) for r in available)
        raise ProjectError(
            f"有 {len(available)} 個專案（{listed}），相對路徑會不知道要建在哪。"
            "請指定 project，或直接給絕對路徑。")
    return os.path.abspath(os.path.join(available[0], spec)), available[0]


def add_project(directory: str, csd_root: "str | None" = None) -> dict:
    """把一個專案寫進 csdview 的登錄檔，兩邊共用同一份設定。"""
    if not directory or not isinstance(directory, str):
        # 少給或給錯型別時，os.path.abspath(None) 會丟 TypeError 掉進 catch-all
        raise ProjectError(
            "請給 directory：專案根目錄的絕對路徑（.ccs 或 cocosstudio 所在的那一層）。")
    if csd_root is not None and not isinstance(csd_root, str):
        raise ProjectError(f"csdRoot 要是路徑字串，收到 {type(csd_root).__name__}。")
    directory = os.path.abspath(directory)
    if not os.path.isdir(directory):
        raise ProjectError(f"資料夾不存在：{directory}")
    root = os.path.abspath(csd_root) if csd_root else detect_root(directory)
    if not root or not os.path.isdir(root):
        raise ProjectError(
            f"在 {directory} 底下找不到 cocosstudio 根目錄，請用 csdRoot 明確指定。")

    try:
        with file_lock(REGISTRY):
            entries = [e for e in load_registry()
                       if _norm(e["directory"]) != _norm(directory)]
            entries.append({"directory": directory, "name": "CLAUDE.md", "csdRoot": root})
            _write_registry(entries)
    except LockTimeout as exc:
        raise ProjectError(str(exc)) from exc
    return {"directory": directory, "csdRoot": root,
            "csdCount": len(index_root(root)), "registry": REGISTRY}
