"""每次寫入前先存一份快照，讓任何改動都能還原。

快照放在系統暫存區、依檔案絕對路徑分桶，不碰專案資料夾——
.csd 都在 git 裡，在旁邊丟一堆 .bak 會污染工作區與 git status。

存的是「改動前」的內容，所以 undo 一次就是退回上一步。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time

from .locking import ReplaceBusy, file_lock, replace_with_retry

LIMIT = 60          # 每個檔案保留幾份快照


def _bucket(path: str) -> str:
    key = hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()[:16]
    folder = os.path.join(tempfile.gettempdir(), "csdmcp_history", key)
    os.makedirs(folder, exist_ok=True)
    return folder


def _meta_path(path: str) -> str:
    return os.path.join(_bucket(path), "meta.json")


def _head_path(path: str) -> str:
    return os.path.join(_bucket(path), "head.sha")


def _sha(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _read_head(path: str) -> "str | None":
    try:
        with open(_head_path(path), encoding="utf-8") as fh:
            value = fh.read().strip()
        return value or None
    except OSError:
        return None


def mark_head(path: str, data: bytes) -> None:
    """記下最近一次由這個工具寫入的內容指紋，undo 用來判斷檔案有沒有被別人改過。"""
    folder = _bucket(path)
    with file_lock(_meta_path(path)):
        handle, temp = tempfile.mkstemp(dir=folder, suffix=".sha.tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(_sha(data))
            replace_with_retry(temp, _head_path(path))
        except BaseException:
            if os.path.exists(temp):
                os.remove(temp)
            raise


def check_head(path: str) -> None:
    """檔案內容若跟上次寫入的不一樣，就拒絕覆蓋。

    沒有指紋（舊的還原紀錄、或還沒被這個工具寫過）就略過，行為跟以前一樣。
    """
    expected = _read_head(path)
    if expected is None:
        return
    try:
        with open(path, "rb") as fh:
            current = _sha(fh.read())
    except OSError as exc:
        raise ValueError(
            f"{os.path.basename(path)} 讀不到，還原沒有發生：{exc}") from exc
    if current != expected:
        raise ValueError(
            f"{os.path.basename(path)} 在還原之前被別的程式改過"
            "（可能是 Cocos Studio 存檔，或另一個 agent）。"
            "為了不蓋掉對方的改動，這次沒有還原，原檔沒有動。"
            "請重新讀一次現在的內容再決定要不要還原。")


def _load(path: str) -> list:
    try:
        with open(_meta_path(path), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return []


def _save(path: str, entries: list) -> None:
    """原子寫入紀錄檔。

    直接 open+write 的話，寫到一半被中斷就留下半截 JSON；而 `_load` 會把解析錯誤
    當成「沒有紀錄」吞掉，於是**整份還原紀錄靜默消失**——偏偏是最需要它的時候。
    """
    target = _meta_path(path)
    handle, temp = tempfile.mkstemp(dir=os.path.dirname(target), suffix=".json.tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(entries, fh, ensure_ascii=False, indent=2)
        replace_with_retry(temp, target)
    except BaseException:
        if os.path.exists(temp):
            os.remove(temp)
        raise


def _claim_serial(folder: str, entries: list) -> tuple:
    """搶一個沒被用過的序號，回傳 (序號, 檔名)。

    用 O_EXCL 讓檔案系統決定誰搶到。單看紀錄檔算 serial+1 的話，
    兩個行程會算出同一個號碼、把對方的快照覆蓋掉。
    """
    serial = (max(e["serial"] for e in entries) + 1) if entries else 1
    while True:
        name = f"{serial:04d}.csd"
        try:
            fd = os.open(os.path.join(folder, name),
                         os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
        except FileExistsError:
            serial += 1
            continue
        os.close(fd)
        return serial, name


def snapshot(path: str, description: str) -> dict:
    """把目前的內容存一份，回傳這筆紀錄。"""
    folder = _bucket(path)
    with file_lock(_meta_path(path)):
        entries = _load(path)
        serial, name = _claim_serial(folder, entries)
        shutil.copyfile(path, os.path.join(folder, name))
        record = {
            "serial": serial,
            "file": name,
            "description": description,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "size": os.path.getsize(path),
        }
        entries.append(record)

        for stale in entries[:-LIMIT]:
            try:
                os.remove(os.path.join(folder, stale["file"]))
            except OSError:
                pass
        _save(path, entries[-LIMIT:])
    return record


def history(path: str) -> list:
    return _load(path)


def undo(path: str, steps: int = 1) -> dict:
    """退回 steps 步之前的內容。

    還原本身也會先存一份快照，所以「還原」這個動作同樣可以再被還原。
    若檔案在上次寫入之後被別人改過（Cocos Studio、另一個 agent），拒絕覆蓋。
    """
    entries = _load(path)
    if not entries:
        raise ValueError(f"{os.path.basename(path)} 沒有可還原的紀錄（這個工具還沒改過它）。")
    steps = max(1, int(steps))
    if steps > len(entries):
        raise ValueError(f"只有 {len(entries)} 筆可還原的紀錄，要不了 {steps} 步。")

    target = entries[-steps]
    source = os.path.join(_bucket(path), target["file"])
    if not os.path.isfile(source):
        raise ValueError(f"快照檔不見了：{source}")

    check_head(path)
    snapshot(path, f"還原到第 {target['serial']} 號快照之前的狀態")
    # 快照要花時間，換檔之前再確認一次，把空窗縮到最小。
    # 中間那一小段 Cocos Studio 仍可能存檔——它不理會我們的鎖，.csd 旁邊
    # 也不能放 .lock（會污染工作區）；這是已知取捨，跟 save() 同一道窗。
    check_head(path)
    # 先寫暫存再換檔：直接覆蓋原檔的話，中途失敗會留下寫到一半的 .csd
    folder = os.path.dirname(path)
    handle, temp = tempfile.mkstemp(dir=folder, suffix=".csd.tmp")
    try:
        with open(source, "rb") as src:
            restored = src.read()
        with os.fdopen(handle, "wb") as fh:
            fh.write(restored)
        check_head(path)
        replace_with_retry(temp, path)
    except ReplaceBusy as exc:
        if os.path.exists(temp):
            os.remove(temp)
        # 這裡不能用 EditError（會反過來 import editor），undo 的錯誤本來就走 ValueError
        raise ValueError(f"{exc} 還原沒有發生，檔案維持原狀，快照也還留著。") from exc
    except BaseException:
        if os.path.exists(temp):
            os.remove(temp)
        raise
    mark_head(path, restored)
    return {"restored": target, "steps": steps}
