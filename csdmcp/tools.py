"""MCP 工具的定義與實作。

檢視類的工具轉呼叫 csdview（見 view.py），編輯類的走自己的編輯核心（見 editor.py）。

兩個貫穿的設計：

- **一次呼叫是一筆交易。** 所有編輯工具底下都是同一個 `apply_ops`：
  依序套用、全部成功才驗證、驗證過才備份寫檔。中途出錯就整筆放棄，原檔不動。
- **改完可以順便看。** 編輯工具的 `preview` 打開時，存檔後直接渲染一張圖回傳，
  不必再叫一次 render。這是 MCP 相對於 CLI 真正省事的地方。
"""
from __future__ import annotations

import os

from . import ccs, history, projects, view
from .editor import Csd, EditError, NODE_TYPES, create as editor_create
from .projects import ProjectError

_PROJECT = {"type": "string",
            "description": "專案名稱或資料夾（可省略；省略時在所有已登錄專案裡找）"}
_CSD = {"type": "string",
        "description": "csd 檔案：絕對路徑、相對於 cocosstudio 根的路徑，或只給檔名"}
_NODE = {"type": "string",
         "description": "節點路徑，例如 Layer/panel/btn_ok；也接受尾段路徑、單一名稱或 @ActionTag"}

_PROPS_DOC = (
    "要改的屬性。位置尺寸 x/y/w/h、相對位移 dx/dy、錨點 anchorX/anchorY、"
    "縮放 scaleX/scaleY、旋轉 rotation、百分比 preX/preY/preW/preH；"
    "外觀 alpha(0-255)/visible/color('#RRGGBB')/colorAlpha/flipX/flipY；"
    "內容 text/fontSize/image/plist/pressedImage/disabledImage；"
    "其他 name/tag/zOrder/touchEnabled/clipAble/blendAdditive。"
    "樣板沒涵蓋的欄位用 attrs（節點屬性）或 elements（子元素屬性），值給 null 代表刪掉。"
)

_PROPS = {"type": "object", "description": _PROPS_DOC, "additionalProperties": True}


def _preview_field(default_text: str) -> dict:
    return {"type": "boolean", "default": False,
            "description": f"存檔後順便渲染一張圖回傳（{default_text}）"}


TOOLS = [
    # ------------------------------------------------------------ 專案
    {
        "name": "list_projects",
        "description": "列出認得的 Cocos Studio 專案與各自的 cocosstudio 根目錄。"
                       "來源是 csdview 的登錄檔、CSDMCP_ROOTS 環境變數與當前工作目錄。"
                       "不確定要對哪個專案動手時先叫這個。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_project",
        "description": "把一個新的專案登錄進來（寫進 csdview 的登錄檔，兩邊共用）。"
                       "換到別的遊戲專案或別的 Cocos Studio 專案時用這個。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "專案資料夾"},
                "csdRoot": {"type": "string",
                            "description": "cocosstudio 根目錄（省略則自動偵測）"},
            },
            "required": ["directory"],
        },
    },
    {
        "name": "list_csd",
        "description": "列出專案底下的 .csd 檔案，可用關鍵字過濾。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": _PROJECT,
                "filter": {"type": "string", "description": "只留下路徑含這段字的檔案"},
                "limit": {"type": "integer", "default": 80},
            },
        },
    },

    # ------------------------------------------------------------ 檢視
    {
        "name": "info",
        "description": "檔案結構、動畫清單與長度、設計解析度、引用到的其他 .csd。"
                       "開始處理一個檔案前先看這個。",
        "inputSchema": {"type": "object",
                        "properties": {"csd": _CSD, "project": _PROJECT},
                        "required": ["csd"]},
    },
    {
        "name": "tree",
        "description": "節點階層與每個節點在畫面上的位置。編輯前用它取得正確的節點路徑。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "csd": _CSD, "project": _PROJECT,
                "anim": {"type": "string", "description": "動畫名稱"},
                "frame": {"type": "number", "description": "影格（指定 anim 時是該段的第幾格）"},
                "depth": {"type": "integer", "default": 99,
                          "description": "只回到第幾層（根算第 0 層）。整棵樹在大檔上"
                                         "很長，先用 2 看骨架，被裁掉的節點會回一個 "
                                         "childCount 告訴你底下還有幾個，需要時再往下鑽。"},
            },
            "required": ["csd"],
        },
    },
    {
        "name": "find",
        "description": "依名稱或型別搜尋節點，回報畫面座標。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "csd": _CSD, "project": _PROJECT,
                "name": {"type": "string", "description": "名稱關鍵字"},
                "type": {"type": "string",
                         "description": "型別關鍵字，例如 Button / Sprite / Text"},
                "anim": {"type": "string"}, "frame": {"type": "number"},
            },
            "required": ["csd"],
        },
    },
    {
        "name": "spec",
        "description": "每個節點的座標、尺寸、貼圖路徑、文字內容。移植後做結構化比對用。"
                       "整份在大檔上很長，只要比某一塊就用 node 或 depth 縮小範圍。",
        "inputSchema": {
            "type": "object",
            "properties": {"csd": _CSD, "project": _PROJECT,
                           "anim": {"type": "string"}, "frame": {"type": "number"},
                           "node": {"type": "string",
                                    "description": "只出這個節點與它的子孫"
                                                   "（節點路徑，例如 Layer/panel）"},
                           "depth": {"type": "integer", "default": 99,
                                     "description": "深度上限；配合 node 時從該節點算"
                                                    "第 0 層。有篩選時回傳會多出 "
                                                    "totalNodeCount 告訴你整份有幾個節點。"}},
            "required": ["csd"],
        },
    },
    {
        "name": "doctor",
        "description": "檢查缺失資源、staleLayout（百分比與絕對值對不上）與 textOverflow。"
                       "畫面缺東西或版面怪怪的先跑這個；編輯完也該再跑一次確認沒弄壞。",
        "inputSchema": {"type": "object",
                        "properties": {"csd": _CSD, "project": _PROJECT},
                        "required": ["csd"]},
    },
    {
        "name": "render",
        "description": "渲染一格畫面，圖片直接回傳（同時也會存成 PNG，路徑一併回報）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "csd": _CSD, "project": _PROJECT,
                "anim": {"type": "string"},
                "frame": {"type": "number", "description": "指定 anim 時是該段動畫的第幾格"},
                "time": {"type": "number", "description": "改用秒數指定"},
                "scale": {"type": "number", "description": "輸出縮放倍率"},
                "annotate": {"type": "boolean", "default": False,
                             "description": "疊上節點外框與名稱"},
                "bg": {"type": "string", "description": "背景：checker、black、白色碼等"},
                "hide": {"type": "array", "items": {"type": "string"},
                         "description": "強制隱藏這些節點路徑"},
                "show": {"type": "array", "items": {"type": "string"},
                         "description": "強制顯示（連 .csd 裡設為隱藏的也叫得出來）"},
                "noParticles": {"type": "boolean", "default": False,
                                "description": "不畫粒子標示框"},
                "designOnly": {"type": "boolean", "default": False,
                               "description": "只畫設計區，不含畫布外的內容"},
            },
            "required": ["csd"],
        },
    },
    {
        "name": "compare",
        "description": "跟一張參考圖（例如 Cocos Creator 截圖）比對，回報 verdict 與差異最大的節點。"
                       "先讀 hints，它會在前提失效時直接告訴你其他數字不可信。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "csd": _CSD, "project": _PROJECT,
                "reference": {"type": "string", "description": "參考圖路徑"},
                "anim": {"type": "string"}, "frame": {"type": "number"},
                "diffBg": {"type": "string",
                           "description": "背景色，要跟截圖一致（預設純黑）"},
            },
            "required": ["csd", "reference"],
        },
    },

    # ------------------------------------------------------------ 建立新檔
    {
        "name": "create_csd",
        "description": "從零建立一份新的 .csd，格式與 Cocos Studio 存出來的完全一致。"
                       "預設會同時登錄進專案的 .ccs——沒登錄的話用 Cocos Studio "
                       "開專案時資源列表看不到這個檔案。可以用 nodes 一次把內容也建好。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "要建在哪：相對於 cocosstudio 根的路徑"
                                        "（例如 myModule/Csd/Panel.csd）或絕對路徑"},
                "project": _PROJECT,
                "rootType": {"type": "string", "enum": ["node", "layer"], "default": "node",
                             "description": "layer 有固定畫布（設計解析度），"
                                            "node 沒有、尺寸由內容決定"},
                "width": {"type": "number", "default": 0,
                          "description": "根節點尺寸；rootType 是 layer 時就是設計解析度"},
                "height": {"type": "number", "default": 0},
                "rootName": {"type": "string",
                             "description": "根節點名稱（預設 Layer 或 Node）"},
                "register": {"type": "boolean", "default": True,
                             "description": "登錄進 .ccs 專案檔"},
                "overwrite": {"type": "boolean", "default": False,
                              "description": "檔案已存在時是否覆蓋"},
                "nodes": {"type": "array",
                          "description": "建好之後接著要新增的節點，"
                                         "格式同 edit_batch 的 add 操作（可省略 op）",
                          "items": {"type": "object", "additionalProperties": True}},
                "preview": _preview_field("看新檔案長什麼樣"),
                "anim": {"type": "array",
                         "description": "建好節點後接著寫的時間軸，格式同 set_anim"
                                        "（每筆要有 node，加上 prop+frames 或 tracks）",
                         "items": {"type": "object", "additionalProperties": True}},
            },
            "required": ["path"],
        },
    },
    {
        "name": "register_csd",
        "description": "把一個已存在的 .csd 登錄進 .ccs 專案檔（或用 remove 取消登錄）。"
                       "手動複製檔案進專案、或 Cocos Studio 裡看不到某個檔案時用這個。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "csd": _CSD, "project": _PROJECT,
                "remove": {"type": "boolean", "default": False,
                           "description": "改成從 .ccs 裡移除這一筆"},
            },
            "required": ["csd"],
        },
    },

    # ------------------------------------------------------------ 編輯
    {
        "name": "set_prop",
        "description": "改一個節點的屬性。改到位置或尺寸時會自動同步 Cocos Studio 的"
                       "冗餘欄位（四個邊界、PrePosition、PreSize），改尺寸還會重算子節點的，"
                       "所以不會做出 doctor 會報 staleLayout 的檔案。",
        "inputSchema": {
            "type": "object",
            "properties": {"csd": _CSD, "project": _PROJECT, "node": _NODE,
                           "props": _PROPS, "preview": _preview_field("看改完的樣子")},
            "required": ["csd", "node", "props"],
        },
    },
    {
        "name": "add_node",
        "description": "在某個父節點底下新增節點。型別："
                       + "、".join(sorted(NODE_TYPES)) + "。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "csd": _CSD, "project": _PROJECT,
                "parent": {"type": "string",
                           "description": "父節點路徑（省略則加在根節點底下）"},
                "type": {"type": "string", "enum": sorted(NODE_TYPES), "default": "sprite"},
                "name": {"type": "string", "description": "新節點的名稱，同層不可重複"},
                "props": _PROPS,
                "preview": _preview_field("看新節點加在哪"),
            },
            "required": ["csd", "name"],
        },
    },
    {
        "name": "delete_node",
        "description": "刪掉一個節點與它的整棵子樹，同時清掉指向它們的動畫時間軸。",
        "inputSchema": {
            "type": "object",
            "properties": {"csd": _CSD, "project": _PROJECT, "node": _NODE,
                           "preview": _preview_field("確認刪對了")},
            "required": ["csd", "node"],
        },
    },
    {
        "name": "duplicate_node",
        "description": "複製一個節點（含子樹），並配上全新的 ActionTag 與 Tag。"
                       "ActionTag 一定要換，否則兩個節點會被同一段動畫驅動。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "csd": _CSD, "project": _PROJECT, "node": _NODE,
                "name": {"type": "string", "description": "新名稱（省略則自動加序號）"},
                "dx": {"type": "number", "default": 0, "description": "複本相對原節點的位移"},
                "dy": {"type": "number", "default": 0},
                "withAnimation": {"type": "boolean", "default": False,
                                  "description": "連時間軸一起複製一份給新的 ActionTag"},
                "preview": _preview_field("看複本的位置"),
            },
            "required": ["csd", "node"],
        },
    },
    {
        "name": "reparent",
        "description": "把節點搬到另一個父層底下。預設會重算座標讓它在畫面上待在原地。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "csd": _CSD, "project": _PROJECT, "node": _NODE,
                "parent": {"type": "string", "description": "新的父節點路徑"},
                "keep": {"type": "string", "enum": ["screen", "local"], "default": "screen",
                         "description": "screen=畫面位置不變（預設）；local=保留原本的座標數字"},
                "preview": _preview_field("確認位置沒跑掉"),
            },
            "required": ["csd", "node", "parent"],
        },
    },
    {
        "name": "edit_batch",
        "description": "一次做多個編輯，全部成功才寫檔，中途出錯就整筆放棄、原檔不動。"
                       "要一起改好幾個節點時用這個，只會產生一份快照。"
                       "op 可以是 set / add / delete / duplicate / reparent / anim / clips。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "csd": _CSD, "project": _PROJECT,
                "ops": {
                    "type": "array",
                    "description": "依序套用的操作。每筆的 op 是 "
                                   "set / add / delete / duplicate / reparent / anim / clips，"
                                   "其餘欄位跟同名的獨立工具一樣。",
                    "items": {"type": "object", "additionalProperties": True},
                },
                "preview": _preview_field("看整批改完的樣子"),
            },
            "required": ["csd", "ops"],
        },
    },

    # ------------------------------------------------------------ 動畫
    {
        "name": "list_anim",
        "description": "讀出時間軸：每條軌道綁哪個節點、哪個屬性、有哪些關鍵影格。"
                       "寫動畫前先看這個；也可以指定 node 只看那一個節點。",
        "inputSchema": {
            "type": "object",
            "properties": {"csd": _CSD, "project": _PROJECT, "node": _NODE},
            "required": ["csd"],
        },
    },
    {
        "name": "set_anim",
        "description": "寫一條（或同一個節點的多條）動畫軌道。frames 是空的就刪掉那條軌道。"
                       "Duration 會自動設成最晚關鍵影格；要留空白尾巴再傳 duration。"
                       "屬性：position / scale / rotation / anchor / alpha / color / "
                       "visible / image / blend / event / inner。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "csd": _CSD, "project": _PROJECT,
                "node": _NODE,
                "prop": {"type": "string",
                         "description": "要寫的屬性，例如 position、scale、alpha、image"},
                "frames": {
                    "type": "array",
                    "description": "關鍵影格。每格一定要有 frame；"
                                   "其餘欄位依屬性而定（x/y、scaleX/scaleY、rotation、"
                                   "alpha、color、image/plist、visible、easing、tween）。"
                                   "省略的座標從上一格（或節點目前的值）接著用。",
                    "items": {"type": "object", "additionalProperties": True},
                },
                "tracks": {
                    "type": "object",
                    "description": "一次寫多條軌道，例如 "
                                   "{\"position\": [...], \"scale\": [...]}。"
                                   "有 tracks 就不必再傳 prop/frames。",
                    "additionalProperties": True,
                },
                "duration": {"type": "integer",
                             "description": "時間軸總長度（格）。省略則等於最晚的關鍵影格。"},
                "speed": {"type": "number", "description": "播放速度，預設 1"},
                "clips": {
                    "type": "array",
                    "description": "具名片段 AnimationList，"
                                   "每筆 {name, start, end}。空陣列會刪掉清單。",
                    "items": {"type": "object", "additionalProperties": True},
                },
                "preview": _preview_field("看某一格的畫面"),
                "anim": {"type": "string", "description": "preview 時用的片段名稱"},
                "frame": {"type": "number", "description": "preview 時渲染第幾格"},
            },
            "required": ["csd"],
        },
    },

    # ------------------------------------------------------------ 還原
    {
        "name": "history",
        "description": "列出這個檔案被改過幾次、每次改了什麼。",
        "inputSchema": {"type": "object",
                        "properties": {"csd": _CSD, "project": _PROJECT},
                        "required": ["csd"]},
    },
    {
        "name": "undo",
        "description": "退回前幾步的內容。還原本身也會存快照，所以還原也能再還原——"
                       "也就是說**連續呼叫會在兩個狀態之間來回**（第二次等於重做）。"
                       "要一次退回好幾步就用 steps，不要連續呼叫；不確定退到哪裡先看 history。",
        "inputSchema": {
            "type": "object",
            "properties": {"csd": _CSD, "project": _PROJECT,
                           "steps": {"type": "integer", "default": 1}},
            "required": ["csd"],
        },
    },
]


# ---------------------------------------------------------------- 實作

def _resolve(args: dict) -> str:
    return projects.resolve_csd(args.get("csd"), args.get("project"))


def _frame_args(args: dict) -> list:
    return view.frame_args(args)


def _anim_ops(args: dict) -> list:
    """把 set_anim 的參數拆成 apply_ops 吃的操作。"""
    ops = []
    if args.get("tracks"):
        if not args.get("node"):
            raise EditError("寫 tracks 時一定要給 node。")
        ops.append({"op": "anim", "node": args["node"], "tracks": args["tracks"]})
    elif args.get("prop") is not None or args.get("frames") is not None:
        if not args.get("node"):
            raise EditError("寫時間軸時一定要給 node。")
        if not args.get("prop"):
            raise EditError("請給 prop，或改用 tracks 一次寫多條。")
        ops.append({"op": "anim", "node": args["node"],
                    "prop": args["prop"], "frames": args.get("frames") or []})
    if args.get("clips") is not None:
        ops.append({"op": "clips", "clips": args["clips"]})
    if args.get("speed") is not None:
        ops.append({"op": "speed", "speed": args["speed"]})
    if args.get("duration") is not None:
        ops.append({"op": "duration", "duration": args["duration"]})
    if not ops:
        raise EditError(
            "set_anim 沒有事情可做。請給 prop+frames、tracks、clips、duration 或 speed。")
    return ops


def _shape(ops) -> list:
    """把外面傳進來的 ops 檢查成「一串物件」。

    這些值是模型直接給的，型別完全不保證。不擋的話 `op.get(...)` 會丟
    AttributeError、`props` 給成陣列會在組錯誤訊息時丟 TypeError，
    兩種都掉進 server 的 catch-all，模型只看到「未預期的錯誤」而不知道哪裡寫錯。
    """
    if isinstance(ops, dict):
        ops = [ops]                     # 只給一筆時包成串，這是好猜的意圖
    if not isinstance(ops, list):
        raise EditError(f"ops 要是一個陣列，收到 {type(ops).__name__}。")
    for index, op in enumerate(ops, 1):
        if not isinstance(op, dict):
            raise EditError(
                f"第 {index} 筆操作要是物件（至少要有 op 欄位），"
                f"收到 {type(op).__name__}：{op!r:.40}")
        props = op.get("props")
        if props is not None and not isinstance(props, dict):
            raise EditError(
                f"第 {index} 筆操作的 props 要是物件（屬性名對值），"
                f"收到 {type(props).__name__}：{props!r:.40}")
    return ops


def _apply_ops(path: str, ops: list) -> dict:
    """依序套用所有操作。任何一步失敗就整筆放棄——這時還沒寫過檔，原檔本來就沒動。"""
    ops = _shape(ops)
    doc = Csd(path)
    results: list = []
    for index, op in enumerate(ops, 1):
        kind = str(op.get("op", "set")).strip().lower()
        try:
            if kind == "set":
                results.append(doc.set_props(doc.resolve(op.get("node")),
                                             op.get("props") or {}))
            elif kind == "add":
                parent = doc.resolve(op.get("parent"))
                results.append(doc.add_node(parent, op.get("type", "sprite"),
                                            op.get("name"), op.get("props") or {}))
            elif kind == "delete":
                results.append(doc.delete_node(doc.resolve(op.get("node"))))
            elif kind == "duplicate":
                results.append(doc.duplicate_node(
                    doc.resolve(op.get("node")), op.get("name"),
                    float(op.get("dx") or 0), float(op.get("dy") or 0),
                    bool(op.get("withAnimation"))))
            elif kind == "reparent":
                results.append(doc.reparent(doc.resolve(op.get("node")),
                                            doc.resolve(op.get("parent")),
                                            op.get("keep", "screen")))
            elif kind == "anim":
                from . import anim as anim_mod
                ref = doc.resolve(op.get("node"))
                if op.get("tracks"):
                    results.append({"tracks": anim_mod.set_tracks(doc, ref, op["tracks"])})
                else:
                    results.append(anim_mod.set_track(
                        doc, ref, op.get("prop"), op.get("frames") or []))
            elif kind == "clips":
                from . import anim as anim_mod
                results.append(anim_mod.set_clips(doc, op.get("clips") or []))
            elif kind == "speed":
                from . import anim as anim_mod
                anim_mod.set_speed(doc, float(op["speed"]))
                results.append({"speed": float(op["speed"])})
            elif kind == "duration":
                from . import anim as anim_mod
                results.append(anim_mod.set_duration(doc, op["duration"]))
            else:
                raise EditError(
                    f"不認得的操作 '{kind}'，可用的是 "
                    "set / add / delete / duplicate / reparent / anim / clips。")
        except EditError as exc:
            raise EditError(f"第 {index} 個操作（{kind}）失敗，整批都沒有套用：{exc}") from exc

    description = "；".join(doc.changes) or "編輯"
    saved = doc.save(description[:300])
    return {"file": path, "operations": results, **saved}


def _with_preview(payload: dict, args: dict, path: str) -> tuple:
    """需要的話補一張渲染圖，回傳 (payload, 圖片或 None)。"""
    if not args.get("preview"):
        return payload, None
    try:
        meta, image = view.render(path, {"anim": args.get("anim"),
                                         "frame": args.get("frame"),
                                         "annotate": args.get("annotate", False)})
        payload["preview"] = {"image": meta.get("image"),
                              "imageSize": meta.get("imageSize"),
                              "missingAssets": meta.get("missingAssets") or []}
        return payload, image
    except view.ViewError as exc:
        payload["previewError"] = str(exc)
        return payload, None


def call(name: str, args: dict) -> tuple:
    """執行一個工具，回傳 (要給模型看的 JSON, 內嵌圖片或 None)。"""
    args = args or {}

    if name == "list_projects":
        entries = projects.known_projects()
        return {"projects": entries, "count": len(entries),
                "registry": projects.REGISTRY,
                "csdview": view.available()}, None

    if name == "add_project":
        return projects.add_project(args.get("directory"), args.get("csdRoot")), None

    if name == "list_csd":
        needle = (args.get("filter") or "").lower()
        limit = int(args.get("limit") or 80)
        found: list = []
        for root in projects.roots(args.get("project")):
            for rel, full in sorted(projects.index_root(root).items()):
                if needle and needle not in rel.lower():
                    continue
                found.append({"path": rel, "root": root, "file": full})
        return {"files": found[:limit], "count": len(found),
                "truncated": len(found) > limit}, None

    if name in ("info", "doctor"):
        return view.run([name, _resolve(args)]), None

    if name == "tree":
        path = _resolve(args)
        return view.run(["tree", path, view.opt("--depth", args.get("depth", 99))]
                        + _frame_args(args)), None

    if name == "find":
        path = _resolve(args)
        command = ["find", path]
        if args.get("name"):
            command.append(view.opt("--name", args["name"]))
        if args.get("type"):
            command.append(view.opt("--type", args["type"]))
        return view.run(command + _frame_args(args)), None

    if name == "spec":
        command = ["spec", _resolve(args)] + _frame_args(args)
        if args.get("node"):
            command.append(view.opt("--node", args["node"]))
        if args.get("depth") is not None:
            command.append(view.opt("--depth", args["depth"]))
        return view.run(command), None

    if name == "render":
        path = _resolve(args)
        meta, image = view.render(path, args)
        return meta, image

    if name == "compare":
        path = _resolve(args)
        reference = args.get("reference")
        if not reference or not os.path.isfile(reference):
            raise ProjectError(f"參考圖不存在：{reference}")
        return view.compare(path, reference, args), None

    if name == "create_csd":
        path, root = projects.resolve_new(args.get("path"), args.get("project"))
        solution = ccs.find_solution(root) if root else None
        made = editor_create(
            path, args.get("rootType", "node"),
            float(args.get("width") or 0), float(args.get("height") or 0),
            args.get("rootName"), ccs.project_version(solution),
            bool(args.get("overwrite")))
        projects.forget_index(root)         # 讓新檔立刻找得到

        ops = []
        if args.get("nodes"):
            ops.extend({**op, "op": op.get("op", "add")} for op in args["nodes"])
        if args.get("anim"):
            for item in args["anim"]:
                ops.append({**item, "op": item.get("op", "anim")})
        if ops:
            made["content"] = _apply_ops(path, ops)

        if args.get("register", True):
            if not solution:
                made["registered"] = False
                made["registerNote"] = (
                    f"找不到 {root} 對應的 .ccs，只建了檔案。"
                    "Cocos Studio 的資源列表不會顯示它，要手動加進專案。")
            else:
                try:
                    made["registered"] = ccs.register(
                        solution, root, path, made["rootType"])
                except ccs.CcsError as exc:
                    made["registered"] = False
                    made["registerNote"] = f"檔案建好了，但登錄 .ccs 失敗：{exc}"
        return _with_preview(made, args, path)

    if name == "register_csd":
        path = _resolve(args)
        root = next((r for r in projects.roots(args.get("project"))
                     if os.path.normcase(path).startswith(os.path.normcase(r) + os.sep)),
                    None)
        if not root:
            raise ProjectError(f"{path} 不在任何已知專案的 cocosstudio 根目錄底下。")
        solution = ccs.find_solution(root)
        if not solution:
            raise ProjectError(f"找不到 {root} 對應的 .ccs 專案檔。")
        if args.get("remove"):
            return ccs.unregister(solution, root, path), None
        from .xmldoc import Xml
        with open(path, "rb") as fh:
            head = Xml(fh.read())
        group = head.find_first("PropertyGroup")
        return ccs.register(solution, root, path,
                            (group.get("Type") if group else "Node") or "Node"), None

    if name == "list_anim":
        path = _resolve(args)
        doc = Csd(path)
        ref = doc.resolve(args.get("node")) if args.get("node") else None
        from . import anim as anim_mod
        payload = anim_mod.list_tracks(doc, ref)
        payload["file"] = path
        return payload, None

    if name == "history":
        path = _resolve(args)
        entries = history.history(path)
        return {"file": path, "snapshots": entries, "count": len(entries)}, None

    if name == "undo":
        path = _resolve(args)
        # 先自己轉一次型別：int("一步") 的 ValueError 訊息是
        # 「invalid literal for int() with base 10」，那對模型毫無幫助，
        # 而且它跟下面 history.undo 的 ValueError 混在一起分不出來。
        try:
            steps = int(args.get("steps") or 1)
        except (TypeError, ValueError):
            raise EditError(
                f"steps 要是正整數（往回退幾步），收到 {args.get('steps')!r}。"
                "不確定要退到哪裡先用 history 看快照清單。") from None
        try:
            record = history.undo(path, steps)
        except ValueError as exc:
            # undo 的失敗全都是「使用者做得到的修正」（沒有紀錄、步數太多、檔案被開著），
            # 訊息本身就寫成了怎麼解決。走 ValueError 的話會落到 server 的 catch-all，
            # 模型只會看到一句「未預期的錯誤」，反而看不到那些說明。
            raise EditError(str(exc)) from exc
        # 把「再呼叫一次會發生什麼」寫在回傳裡。光靠工具說明不夠：真正會踩到的
        # 時機是「已經還原了一次、還想再往前」，那一刻模型看的是這個 payload。
        return {"file": path, **record,
                "note": f"再呼叫一次 undo 會變成重做（回到還原前的狀態）。"
                        f"要繼續往前退請用 steps={steps + 1}，或先用 history 看清楚。"}, None

    # ---- 以下都是編輯，共用同一條交易路徑
    if name in ("set_prop", "add_node", "delete_node", "duplicate_node",
                "reparent", "edit_batch", "set_anim"):
        path = _resolve(args)
        if name == "edit_batch":
            ops = args.get("ops") or []
            if not ops:
                raise EditError("ops 是空的，沒有事情可做。")
            ops = _shape(ops)
        elif name == "set_anim":
            ops = _anim_ops(args)
        elif name == "set_prop":
            ops = [{"op": "set", "node": args.get("node"), "props": args.get("props")}]
        elif name == "add_node":
            ops = [{"op": "add", "parent": args.get("parent"),
                    "type": args.get("type", "sprite"), "name": args.get("name"),
                    "props": args.get("props")}]
        elif name == "delete_node":
            ops = [{"op": "delete", "node": args.get("node")}]
        elif name == "duplicate_node":
            ops = [{"op": "duplicate", "node": args.get("node"), "name": args.get("name"),
                    "dx": args.get("dx"), "dy": args.get("dy"),
                    "withAnimation": args.get("withAnimation")}]
        else:
            ops = [{"op": "reparent", "node": args.get("node"),
                    "parent": args.get("parent"), "keep": args.get("keep", "screen")}]
        payload = _apply_ops(path, ops)
        return _with_preview(payload, args, path)

    raise EditError(f"沒有這個工具：{name}")
