"""跨行程的檔案鎖。

用在「只有這個工具會寫、但可能有好幾個行程同時在跑」的檔案上——
快照紀錄與 .ccs 專案檔都屬於這一類。每個 Claude Code 視窗都是一個獨立的伺服器行程，
沒有鎖的話兩邊的 read-modify-write 會互相蓋掉。

`.csd` 本身**不靠鎖保護**：那裡真正的對手是 Cocos Studio，它不會理會我們的鎖。
所以 .csd 走的是「存檔前檢查檔案有沒有被別人動過」的樂觀檢查（見 editor.Csd.save）。

另外附一個 `replace_with_retry`：換檔這一步在 Windows 上會被「正在讀」的行程擋掉，
那跟鎖是同一類問題（跨行程搶同一個檔），放在一起。重試完還是擋著就丟 `ReplaceBusy`，
由呼叫端翻成自己那層的錯誤（EditError / CcsError），因為「所以現在檔案是什麼狀態」
只有呼叫端知道。
"""
from __future__ import annotations

import contextlib
import os
import random
import time

if os.name == "nt":
    import msvcrt

    def _acquire(handle) -> bool:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _release(handle) -> None:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _acquire(handle) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _release(handle) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


# 換檔撞到目標被開著時，總共願意等多久（秒），以及單次退避的上限。
# 用時間預算而不是固定次數：擋住我們的是「別人開著檔的那段時間」，
# 那是時間量，跟我們試了幾次沒有關係。
REPLACE_BUDGET = 2.0
REPLACE_FIRST_WAIT = 0.02
REPLACE_MAX_WAIT = 0.15


class ReplaceBusy(PermissionError):
    """重試完目標還是被別的行程開著。

    刻意獨立成一種類別：呼叫端要能只認這一種情形，把它翻成自己那層的錯誤，
    而不必去分辨其他 PermissionError（例如整個資料夾唯讀）。
    仍然繼承 PermissionError——它本來就是那個錯，既有的 `except PermissionError`
    照樣接得到。
    """


def replace_with_retry(temp: str, target: str,
                       budget: float = REPLACE_BUDGET) -> None:
    """os.replace，撞到目標正被別的行程開著就退避重試，直到用完時間預算。

    Windows 上 os.replace 遇到「目標正被別的行程開著」會直接 PermissionError
    （Python 開檔沒帶 FILE_SHARE_DELETE）。這在這個工具裡是常態不是例外：
    Cocos Studio、csdview 的 watch、另一個 agent 都可能正在讀同一個檔。
    實測有人不停解析同一個檔時，不重試只有 5/60 次寫得進去，加上重試是 60/60。

    為什麼是時間預算而不是固定次數：擋住我們的是「別人開著檔的那一段時間」。
    先前寫成 6 次線性退避（總共約 0.3 秒），實測讀取壓力一大就完全寫不進去——
    無停頓的讀取執行緒 1 個時 14/15、2 個時 5/15、4 個時 0/15。
    只要讀者之間有 1 ms 的空隙就回到 14/15，也就是說缺的不是次數而是耐心。

    退避是指數成長並帶抖動：抖動讓同時退避的多個寫入者不會一起醒來、
    反覆撞在同一個瞬間。沒撞到的時候成本仍然是零（第一次就成功直接回）。

    暫存檔不在這裡清：所有呼叫端都已經用 except BaseException 收尾了。
    撐不過去就丟 ReplaceBusy。訊息只寫「怎麼讓它成功」，不寫「檔案現在是什麼狀態」——
    後者要看是誰在換哪個檔，由呼叫端接上去。
    """
    deadline = time.monotonic() + max(budget, 0.0)
    wait = REPLACE_FIRST_WAIT
    tries = 0
    while True:
        try:
            os.replace(temp, target)
            return
        except PermissionError as exc:
            tries += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReplaceBusy(
                    f"{os.path.basename(target)} 一直被別的程式開著，"
                    f"等了 {budget:g} 秒、試了 {tries} 次都換不進去。"
                    "請關掉正在讀它的程式"
                    "（Cocos Studio、csdview 的 watch、另一個 agent）再試一次。"
                ) from exc
            time.sleep(min(wait * (0.5 + random.random()), remaining))
            wait = min(wait * 1.6, REPLACE_MAX_WAIT)


class LockTimeout(Exception):
    pass


@contextlib.contextmanager
def file_lock(path: str, timeout: float = 10.0):
    """鎖住 `<path>.lock`。

    鎖的是另外一個檔案而不是目標本身：目標會被 os.replace 換掉，
    鎖在被換掉的那個 inode 上等於沒鎖。
    """
    lock_path = path + ".lock"
    folder = os.path.dirname(lock_path)
    if folder:
        os.makedirs(folder, exist_ok=True)

    deadline = time.monotonic() + timeout
    handle = open(lock_path, "a+b")
    try:
        while not _acquire(handle):
            if time.monotonic() > deadline:
                raise LockTimeout(
                    f"等 {timeout:g} 秒還拿不到 {os.path.basename(path)} 的鎖，"
                    "可能有另一個行程正在寫入。稍後再試一次。")
            time.sleep(0.05)
        try:
            yield
        finally:
            _release(handle)
    finally:
        handle.close()
