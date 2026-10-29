"""MCP stdio 伺服器。

刻意不依賴官方 SDK：整個協定在這個情境下只需要 initialize / tools/list / tools/call
三個方法，自己實作就少一個要裝的套件，在沒有對外網路的公司機器上比較不會卡住。

stdout 是協定通道，**只能出現 JSON-RPC 訊息**。任何診斷訊息一律寫 stderr，
不然會把協定弄壞、客戶端只會回報一句看不出原因的解析錯誤。
"""
from __future__ import annotations

import json
import sys
import traceback

from . import tools
from .ccs import CcsError
from .editor import EditError
from .projects import ProjectError
from .view import ViewError
from .xmldoc import XmlError

NAME = "csdview-edit"
VERSION = "1.0.0"

# 客戶端要求的版本若在這裡面就照它的回，否則回我們的預設版本
KNOWN_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_VERSION = "2025-06-18"


def _log(message: str) -> None:
    print(f"[{NAME}] {message}", file=sys.stderr, flush=True)


def _send(payload: dict) -> None:
    line = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(line + b"\n")
    sys.stdout.buffer.flush()


def _result(request_id, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": request_id,
           "error": {"code": code, "message": message}})


def _text(payload) -> dict:
    """把回傳值包成 MCP 的文字內容。

    刻意不排版（沒有 indent）：這段字是給模型讀的，縮排與換行要花 token 而
    不帶任何資訊。實測 indent=2 佔掉整個 payload 的七成——深層嵌套加上
    json 把陣列拆成一個元素一行（`"size": [
  120.0,
  41.0
]`），
    光空白就比資料本身多。JSON 沒有排版一樣解析得動，沒有損失。
    寫到磁碟的那些檔（history、projects 的紀錄）照舊保留 indent，
    那是給人看的、也不佔 token。
    """
    body = (payload if isinstance(payload, str)
            else json.dumps(payload, ensure_ascii=False,
                            separators=(",", ":"), default=str))
    return {"type": "text", "text": body}


def _handle_call(params: dict) -> dict:
    name = params.get("name") or ""
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        # 回 isError 而不是協定層錯誤：模型看得到內容，才有機會自己改對重試。
        return {"content": [_text(
            f"錯誤：arguments 必須是物件（一組參數名對值），收到 "
            f"{type(args).__name__}。請寫成 {{\"csd\": \"…\"}} 這種形式。")],
            "isError": True}
    try:
        payload, image = tools.call(name, args)
    except (EditError, ProjectError, ViewError, CcsError, XmlError) as exc:
        # 這幾種都是「使用者做得到的修正」，訊息本身就寫成了怎麼解決，直接回給模型
        return {"content": [_text(f"錯誤：{exc}")], "isError": True}
    except Exception as exc:                     # noqa: BLE001 - 不能讓伺服器整個掛掉
        _log("未預期的錯誤:\n" + traceback.format_exc())
        return {"content": [_text(f"未預期的錯誤：{type(exc).__name__}: {exc}")],
                "isError": True}

    content = [_text(payload)]
    if image:
        content.append({"type": "image", "data": image["data"], "mimeType": "image/png"})
    return {"content": content, "isError": False}


def handle(message: dict):
    """處理一則 JSON-RPC 訊息。通知（沒有 id）不回覆。"""
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        # 給了 params 但不是物件（字串、陣列……）。以前會一路帶到 params.get 才爆，
        # 掉進 catch-all；照 JSON-RPC 回 -32602 才說得出問題在哪。
        if request_id is not None:
            _error(request_id, -32602,
                   f"params 必須是物件，收到 {type(params).__name__}。")
        return

    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in KNOWN_VERSIONS else DEFAULT_VERSION
        _result(request_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": NAME, "version": VERSION},
            "instructions": (
                "編輯 Cocos Studio 的 .csd，並用 csdview 看實際畫面。"
                "動手前先用 tree 或 find 取得正確的節點路徑；"
                "改完用 doctor 確認沒有做出 staleLayout。"
                "改壞了用 undo 還原。"),
        })
        return

    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return

    if method == "ping":
        _result(request_id, {})
        return

    if method == "tools/list":
        _result(request_id, {"tools": tools.TOOLS})
        return

    if method == "tools/call":
        _result(request_id, _handle_call(params))
        return

    if method == "resources/list":
        # 有些客戶端（含 Cursor）不看 capabilities 就問，回空清單比回錯誤乾淨
        _result(request_id, {"resources": []})
        return

    if method == "resources/templates/list":
        _result(request_id, {"resourceTemplates": []})
        return

    if method == "prompts/list":
        _result(request_id, {"prompts": []})
        return

    if method == "logging/setLevel":
        _result(request_id, {})
        return

    if request_id is not None:
        _error(request_id, -32601, f"不支援的方法: {method}")


def main() -> int:
    _log(f"啟動（{VERSION}）")
    stream = sys.stdin.buffer
    while True:
        line = stream.readline()
        if not line:
            break
        text = line.strip()
        if not text:
            continue
        try:
            message = json.loads(text.decode("utf-8"))
        except ValueError as exc:
            _log(f"收到不是 JSON 的一行，略過：{exc}")
            continue
        # JSON-RPC 的訊息一定是物件。收到陣列（批次請求，這裡不支援）或純量時
        # 要在這裡擋掉：以前直接往下丟，handle 裡的 message.get 會 AttributeError，
        # 而下面那個 except 自己也呼叫 message.get——連 catch-all 一起爆掉，
        # 整個 while 迴圈就結束了。結果是「一行 [1,2,3] 讓伺服器永久沒有回應」。
        if not isinstance(message, dict):
            _log(f"收到不是物件的一則訊息（{type(message).__name__}），略過。"
                 "批次請求（JSON 陣列）目前不支援。")
            continue
        try:
            handle(message)
        except Exception:                        # noqa: BLE001
            _log("處理訊息時發生未預期的錯誤:\n" + traceback.format_exc())
            request_id = message.get("id")
            if request_id is not None:
                _error(request_id, -32603, "伺服器內部錯誤，詳情看 stderr。")
    _log("結束")
    return 0


if __name__ == "__main__":
    sys.exit(main())
