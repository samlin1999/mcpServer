"""在 .csd 上做屬性與結構編輯。

對外的單位是「節點」，不是 XML 元素。呼叫端用 csdview 那套節點路徑
（例如 `Layer/panel/btn_ok`）指定要改哪個節點，這裡負責換算成位元組層級的改動。

三個貫穿整份檔案的規則：

1. **節點一律用 node_path 定位**，也就是「從根算下來第幾個子節點」的索引串。
   每次改動都會讓 XML 元素的位移與索引失效，但 node_path 只描述節點階層，
   插入 `<PrePosition/>` 這種子元素不會影響它。所有操作都在動手前重新解析一次。
2. **任何動到位置或尺寸的操作，都要回頭補上冗餘欄位**（見 layout.derived_fields）。
   漏掉就會做出 csdview `doctor` 會報 `staleLayout` 的檔案。
3. **屬性等於預設值時的寫法跟著原檔**：本來就有這個屬性就照寫，
   本來沒有而新值又剛好是預設值，就不要憑空加一個出來。這樣 diff 最小。
"""
from __future__ import annotations

import os
import random
import re
import tempfile

from . import history, layout, locking
from .layout import CHILD_ORDER, derived_fields, fmt, num
from .xmldoc import Xml, escape_attr

# 每種節點在 .csd 裡的 ctype，以及建立新節點時要補的必要屬性。
# 這些樣板是從專案裡的真實節點抄下來的，不是自己編的欄位。
NODE_TYPES = {
    "node":    ("SingleNodeObjectData", {"IconVisible": "True"}),
    "sprite":  ("SpriteObjectData", {"IconVisible": "False"}),
    "image":   ("ImageViewObjectData", {"IconVisible": "False"}),
    "text":    ("TextObjectData", {"IconVisible": "False", "FontSize": "20",
                                   "LabelText": "文字"}),
    "bmfont":  ("TextBMFontObjectData", {"IconVisible": "False", "LabelText": "0"}),
    "button":  ("ButtonObjectData", {"IconVisible": "False", "TouchEnable": "True",
                                     "FontSize": "20", "ButtonText": "按鈕"}),
    "panel":   ("PanelObjectData", {"IconVisible": "False", "ClipAble": "False",
                                    "BackColorAlpha": "102", "ColorAngle": "90.0000"}),
    "project": ("ProjectNodeObjectData", {"IconVisible": "True",
                                          "InnerActionSpeed": "1.0000"}),
}

# 文字內容存在哪個屬性，依節點型別而定
TEXT_ATTR = {
    "TextObjectData": "LabelText",
    "TextFieldObjectData": "PlaceHolderText",
    "TextBMFontObjectData": "LabelText",
    "ButtonObjectData": "ButtonText",
}

# 圖片資源的元素名稱：一般節點只有一張，按鈕有四張
IMAGE_SLOTS = {
    "image": {"ButtonObjectData": "NormalFileData", "CheckBoxObjectData": "NormalBackFileData",
              "*": "FileData"},
    "pressedImage": {"*": "PressedFileData"},
    "disabledImage": {"*": "DisabledFileData"},
}


class EditError(Exception):
    """編輯失敗；訊息會原樣回給呼叫端，所以要寫成看得懂怎麼修的句子。"""


class NodeRef:
    """一個節點在檔案裡的身分。"""

    __slots__ = ("node_path", "name", "ctype", "path", "depth")

    def __init__(self, node_path, name, ctype, path, depth):
        self.node_path = node_path
        self.name = name
        self.ctype = ctype
        self.path = path
        self.depth = depth

    def __repr__(self):
        return f"<NodeRef {self.path} {self.ctype}>"


class Csd:
    """一份開啟中的 .csd。改動先在記憶體裡累積，save() 才落地。"""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        with open(self.path, "rb") as fh:
            self.original = fh.read()
        # 開檔當下的狀態，存檔前要拿來比對。改動是先在記憶體累積、之後才寫回去的，
        # 這中間別人（另一個 agent、或使用者在 Cocos Studio 裡按存檔）可能也動了同一個檔，
        # 不比對就會無聲蓋掉對方的改動。
        self.opened_as = _stamp(self.path)
        self.xml = Xml(self.original)
        self.xml.attr_order = layout.ATTR_ORDER
        self.changes: list = []
        # 根 ObjectData 的快取。key 是索引物件本身：每次 splice 都會重建索引、
        # 換成新的 root 物件，所以快取會自動失效，不必手動清。
        self._root_cache: tuple = ()

    # ------------------------------------------------------------ 結構走訪

    def _root_elem(self):
        """最外層的 <ObjectData>，也就是這份 .csd 的根節點。

        一次操作裡會被問很多次（nodes()、elem_of()、每條 anim 軌道各一次），
        所以查一次就記住。快取以索引物件當 key，索引重建就自動失效。
        """
        if self._root_cache and self._root_cache[0] is self.xml.root:
            return self._root_cache[1]
        found = self.xml.find_first("ObjectData")
        if found is None:
            raise EditError(f"{os.path.basename(self.path)} 裡找不到 <ObjectData>，"
                            "這個檔案可能不是 .csd。")
        self._root_cache = (self.xml.root, found)
        return found

    def elem_of(self, node_path: tuple):
        """把 node_path 解析成現在的 XML 元素。每次改動後都要重新呼叫。"""
        el = self._root_elem()
        for depth, index in enumerate(node_path):
            container = el.child("Children")
            kids = container.children_named("AbstractNodeData") if container else []
            if index >= len(kids):
                raise EditError(f"節點索引超出範圍（第 {depth + 1} 層要第 {index} 個，"
                                f"只有 {len(kids)} 個）。")
            el = kids[index]
        return el

    def parent_of(self, node_path: tuple) -> tuple:
        if not node_path:
            raise EditError("根節點沒有父層。")
        return node_path[:-1]

    def nodes(self) -> list:
        """把整棵節點樹攤平，路徑格式與 csdview 的 `path` 一致。

        同一層有同名節點時才在名稱後面加 `[n]` 序號；只有一個就不加，
        這樣路徑跟 csdview `tree`／`find` 報出來的完全對得上。
        """
        out: list = []

        # 用明確的堆疊而不是遞迴：節點階層的深度來自檔案內容，損壞的檔案
        # 可以做出上千層，遞迴就會變成 RecursionError（翻不成有用的訊息）。
        root = self._root_elem()
        stack = [(root, (), root.get("Name", ""), 0)]
        while stack:
            el, node_path, path, depth = stack.pop()
            out.append(NodeRef(node_path, el.get("Name", ""),
                               el.get("ctype", "SingleNodeObjectData"), path, depth))
            container = el.child("Children")
            if container is None:
                continue
            kids = container.children_named("AbstractNodeData")
            counts: dict = {}
            for kid in kids:
                key = kid.get("Name", "")
                counts[key] = counts.get(key, 0) + 1
            seen: dict = {}
            labelled = []
            for index, kid in enumerate(kids):
                name = kid.get("Name", "")
                seen[name] = seen.get(name, 0) + 1
                label = f"{name}[{seen[name]}]" if counts[name] > 1 else name
                labelled.append((kid, node_path + (index,),
                                 f"{path}/{label}" if path else label, depth + 1))
            # 反著推，pop 出來才是原本的順序（out 的順序是對外承諾的一部分）
            stack.extend(reversed(labelled))
        return out

    def _elements_in_order(self) -> list:
        """跟 nodes() 同樣順序的 XML 元素，讓兩者可以 zip 起來用。"""
        out: list = []

        stack = [self._root_elem()]
        while stack:
            el = stack.pop()
            out.append(el)
            container = el.child("Children")
            if container is not None:
                stack.extend(reversed(container.children_named("AbstractNodeData")))
        return out

    # ------------------------------------------------------------ 定位節點

    def resolve(self, spec) -> NodeRef:
        """把使用者給的節點描述變成 NodeRef。

        接受完整路徑、省略根節點的路徑、路徑尾段、單一名稱，或 `@ActionTag`。
        對不上就把候選列出來——猜錯節點會改到不該改的東西。
        """
        nodes = self.nodes()
        if spec is None or str(spec).strip() in ("", "/"):
            return nodes[0]
        text = str(spec).strip().replace("\\", "/").strip("/")

        if text.startswith("@") or text.startswith("#"):
            want = text[1:]
            # 走一趟建對照表。原本是對每個節點各呼叫一次 elem_of（每次都從根走下來），
            # 540 KB 的檔案光是查一個 ActionTag 就要 300 ms。
            found = None
            for ref, el in zip(nodes, self._elements_in_order()):
                if el.get("ActionTag") == want:
                    found = ref
                    break
            if found is None:
                raise EditError(f"找不到 ActionTag 為 {want} 的節點。")
            return found

        low = text.lower()
        root_name = nodes[0].path
        # csdview 的 tree／find 不會輸出 [n] 序號，所以照抄過來的路徑對不上同名節點。
        # 比對時把序號拿掉，這種情況才會落到「對應到多個節點」那條有候選清單的錯誤，
        # 而不是含糊的「找不到」。
        bare = _strip_index(text).lower()

        tries = [
            lambda n: n.path == text,
            lambda n: n.path.lower() == low,
            lambda n: n.path == f"{root_name}/{text}",
            lambda n: n.path.lower() == f"{root_name}/{text}".lower(),
            lambda n: _strip_index(n.path).lower() == bare,
            lambda n: _strip_index(n.path).lower() == f"{root_name}/{text}".lower(),
            lambda n: n.path.lower().endswith("/" + low),
            lambda n: _strip_index(n.path).lower().endswith("/" + bare),
            lambda n: n.name.lower() == low,
        ]
        for pred in tries:
            hit = [n for n in nodes if pred(n)]
            if len(hit) == 1:
                return hit[0]
            if len(hit) > 1:
                listed = "\n".join(f"  {n.path}" for n in hit[:12])
                raise EditError(
                    f"'{spec}' 對應到 {len(hit)} 個節點。這些節點同名，"
                    f"請連結尾的序號一起給（例如 {hit[0].path}）：\n{listed}")

        near = [n.path for n in nodes if low in n.path.lower()][:12]
        hint = ("\n相近的節點：\n" + "\n".join(f"  {p}" for p in near)) if near else ""
        raise EditError(f"找不到節點 '{spec}'。用 tree 或 find 取得正確路徑。{hint}")

    # ------------------------------------------------------------ 讀取幾何

    def geometry(self, node_path: tuple) -> dict:
        el = self.elem_of(node_path)
        size, pos = el.child("Size"), el.child("Position")
        anchor, scale = el.child("AnchorPoint"), el.child("Scale")
        rot = num(el.get("Rotation"), 0.0)
        return {
            "w": num(size.get("X") if size else None),
            "h": num(size.get("Y") if size else None),
            "x": num(pos.get("X") if pos else None),
            "y": num(pos.get("Y") if pos else None),
            "ax": num(anchor.get("ScaleX") if anchor else None),
            "ay": num(anchor.get("ScaleY") if anchor else None),
            "sx": num(scale.get("ScaleX") if scale else None, 1.0),
            "sy": num(scale.get("ScaleY") if scale else None, 1.0),
            "rx": num(el.get("RotationSkewX"), rot),
            "ry": num(el.get("RotationSkewY"), rot),
        }

    def parent_size(self, node_path: tuple) -> tuple:
        el = self.elem_of(node_path[:-1]) if node_path else self.elem_of(())
        size = el.child("Size")
        return (num(size.get("X") if size else None), num(size.get("Y") if size else None))

    def child_space_matrix(self, node_path: tuple) -> layout.Mat:
        """從根一路乘下來，得到「這個節點的子座標系 -> 世界」的矩陣。"""
        matrix = layout.Mat()
        for depth in range(len(node_path) + 1):
            g = self.geometry(node_path[:depth])
            matrix = matrix.mul(layout.child_space(
                g["x"], g["y"], g["ax"], g["ay"], g["w"], g["h"],
                g["sx"], g["sy"], g["rx"], g["ry"]))
        return matrix

    # ------------------------------------------------------------ 寫入屬性

    def _collect(self, node_path: tuple, node_attrs: dict, child_attrs: dict) -> list:
        """算出把屬性寫進節點與子元素所需的 cuts，但**不套用**。

        呼叫端負責把整筆編輯的 cuts 收齊後只 splice 一次。每套用一次就要重新解析
        整份檔案，一個欄位一次的話光是改尺寸連帶重算子節點就會做近百次。

        子元素不存在時，直接把整行 `<Tag a="1" b="2" />` 組出來當成插入 cut，
        不走「先建空元素、再設屬性」——那樣每個子元素都會多一次重建。
        """
        cuts: list = []
        el = self.elem_of(node_path)
        # 插入點是依現有子元素算的，所以要先照慣用順序處理，
        # 兩個都不存在的子元素（例如 PrePosition 與 PreSize）才不會前後顛倒
        rank = {name: i for i, name in enumerate(CHILD_ORDER)}
        for tag in sorted(child_attrs, key=lambda t: rank.get(t, len(CHILD_ORDER))):
            # _SKIP 代表「這次沒要動它」；None 代表「把這個屬性刪掉」
            # （跟 _value 的說明一致，也跟 xml.attr_cuts 對 None 的處理一致）
            payload = {k: v for k, v in child_attrs[tag].items() if v is not _SKIP}
            if not payload:
                continue
            target = el.child(tag)
            if target is not None:
                cuts += self.xml.attr_cuts(target.route, payload)
                continue
            # 元素還不存在，就沒有東西可刪；只留下真正要寫進去的值
            payload = {k: v for k, v in payload.items() if v is not None}
            if not payload:
                continue
            made = self.xml.element_cut(el.route, tag, payload, CHILD_ORDER)
            if made is None:
                # 節點本身是自閉合、還沒有任何子元素。展開會動到起始標籤，
                # 跟屬性 cuts 有重疊風險，所以先單獨處理掉再重新取一次元素。
                self.xml.expand_self_closing(el.route)
                el = self.elem_of(node_path)
                made = self.xml.element_cut(el.route, tag, payload, CHILD_ORDER)
            cuts += made

        payload = {k: v for k, v in node_attrs.items() if v is not _SKIP}
        if payload:
            cuts += self.xml.attr_cuts(el.route, payload)
        return cuts


    def _value(self, el, name: str, value, default=None, numeric: bool = True):
        """決定一個屬性該寫成什麼——沿用原檔對「預設值省略」的寫法。

        本來就有這個屬性：照寫新值。
        本來沒有、而新值又等於預設值：不動它（維持省略），避免多出無意義的 diff。

        回傳 _SKIP 表示不要動；回傳 None 是保留給「刪掉這個屬性」用的。
        """
        if value is None:
            return _SKIP
        text = fmt(value) if numeric else str(value)
        if el is not None and name in el.attrib:
            return text
        if default is not None:
            same = (abs(float(value) - float(default)) < 5e-5 if numeric
                    else str(value) == str(default))
            if same:
                return _SKIP
        return text

    def layout_fields(self, node_path: tuple, geom: "dict | None" = None,
                      parent_size: "tuple | None" = None) -> tuple:
        """算出這個節點該有的邊界與百分比欄位，回傳 (節點屬性, 子元素屬性)。

        geom 與 parent_size 可以直接給，不必先把新值寫進檔案再讀回來——
        那樣就非得拆成兩次 splice 不可。
        """
        g = geom if geom is not None else self.geometry(node_path)
        pw, ph = (parent_size if parent_size is not None
                  else self.parent_size(node_path))
        derived = derived_fields(pw, ph, g["x"], g["y"], g["w"], g["h"], g["ax"], g["ay"])

        el = self.elem_of(node_path)
        node_attrs = {key: self._value(el, key, derived[key], default=0.0)
                      for key in layout.MARGIN_KEYS}

        child_attrs = {"PrePosition": {}, "PreSize": {}}
        for tag, axis in (("PrePosition", "X"), ("PrePosition", "Y"),
                          ("PreSize", "X"), ("PreSize", "Y")):
            value = derived[(tag, axis)]
            if value is None:
                # 父層該軸是 0，百分比沒有意義。**不能留著舊值**：絕對座標已經換過，
                # 舊百分比就變成一個互相矛盾的宣告——csdview doctor 會報 staleLayout，
                # 而引擎端可能照百分比把節點擺到別的地方。
                # 移除是對的做法，也正是 Cocos Studio 自己的寫法：真實專案裡父層尺寸
                # 為 0 的節點，6572 個全部都是空的 <PrePosition />（沒有 X/Y 屬性）。
                child_attrs[tag][axis] = None
                # 光拿掉數值不夠：PositionPercentYEnabled 這類旗標若還留著 True，
                # 就等於宣告「這一軸照百分比擺」而百分比是 0——節點會被擺到 0。
                # 撤掉旗標之後，絕對座標成為唯一的依據，這正是 keep="screen" 承諾的。
                for flag in layout.PERCENT_FLAGS[(tag, axis)]:
                    node_attrs[flag] = None
                continue
            child_attrs[tag][axis] = self._value(el.child(tag), axis, value, default=0.0)
        return node_attrs, child_attrs

    def reflow_cuts(self, node_path: tuple, parent_size: tuple) -> tuple:
        """父層尺寸改了，算出直接子節點要跟著重算的 cuts，回傳 (cuts, 子節點數)。

        只有直接子節點需要重算：孫節點的父層（也就是子節點）尺寸並沒有變。
        """
        container = self.elem_of(node_path).child("Children")
        count = len(container.children_named("AbstractNodeData")) if container else 0
        if not count:
            return [], 0
        paths = [node_path + (i,) for i in range(count)]
        self._expand_all(paths)
        cuts: list = []
        for path in paths:
            cuts += self._collect(path, *self.layout_fields(path, parent_size=parent_size))
        return cuts, count

    def _expand_all(self, node_paths: list) -> None:
        """先把還沒有任何子元素的節點展開成一對標籤。

        展開會動到起始標籤、而且必須立刻套用，所以一定要在收集這批 cuts 之前做完——
        收集到一半才展開，先前算好的位移就全部失效了。
        """
        for path in node_paths:
            if self.elem_of(path).self_closing:
                self.xml.expand_self_closing(self.elem_of(path).route)

    # ------------------------------------------------------------ 高階：改屬性

    def set_props(self, ref: NodeRef, props: dict) -> dict:
        """改一個節點的屬性，並維持所有冗餘欄位一致。"""
        node_path = ref.node_path
        el = self.elem_of(node_path)
        ctype = ref.ctype
        before = self.geometry(node_path)
        pw, ph = self.parent_size(node_path)

        unknown = [k for k in props
                   if k not in _KNOWN_PROPS and k not in ("attrs", "elements")]
        if unknown:
            raise EditError(
                f"不認得的屬性：{'、'.join(unknown)}。"
                f"可用的有：{'、'.join(sorted(_KNOWN_PROPS))}；"
                "要改樣板沒涵蓋的欄位請用 attrs / elements。")

        g = dict(before)
        # 百分比先換算成絕對值，之後統一由絕對值推回百分比，兩邊不會打架
        if props.get("preX") is not None and pw:
            g["x"] = float(props["preX"]) * pw
        if props.get("preY") is not None and ph:
            g["y"] = float(props["preY"]) * ph
        if props.get("preW") is not None and pw:
            g["w"] = float(props["preW"]) * pw
        if props.get("preH") is not None and ph:
            g["h"] = float(props["preH"]) * ph
        for key, field in (("x", "x"), ("y", "y"), ("w", "w"), ("h", "h"),
                           ("anchorX", "ax"), ("anchorY", "ay"),
                           ("scaleX", "sx"), ("scaleY", "sy")):
            if props.get(key) is not None:
                g[field] = float(props[key])
        if props.get("dx") is not None:
            g["x"] += float(props["dx"])
        if props.get("dy") is not None:
            g["y"] += float(props["dy"])
        if props.get("rotation") is not None:
            g["rx"] = g["ry"] = float(props["rotation"])

        node_attrs: dict = {}
        child_attrs: dict = {}

        moved = any(abs(g[k] - before[k]) > 1e-6 for k in ("x", "y", "w", "h", "ax", "ay"))
        resized = abs(g["w"] - before["w"]) > 1e-6 or abs(g["h"] - before["h"]) > 1e-6

        if abs(g["w"] - before["w"]) > 1e-6 or abs(g["h"] - before["h"]) > 1e-6:
            child_attrs["Size"] = {"X": fmt(g["w"]), "Y": fmt(g["h"])}
        if abs(g["x"] - before["x"]) > 1e-6 or abs(g["y"] - before["y"]) > 1e-6:
            sub = el.child("Position")
            child_attrs["Position"] = {
                "X": self._value(sub, "X", g["x"], default=0.0),
                "Y": self._value(sub, "Y", g["y"], default=0.0)}
        if abs(g["ax"] - before["ax"]) > 1e-6 or abs(g["ay"] - before["ay"]) > 1e-6:
            sub = el.child("AnchorPoint")
            child_attrs["AnchorPoint"] = {
                "ScaleX": self._value(sub, "ScaleX", g["ax"], default=0.0),
                "ScaleY": self._value(sub, "ScaleY", g["ay"], default=0.0)}
        if abs(g["sx"] - before["sx"]) > 1e-6 or abs(g["sy"] - before["sy"]) > 1e-6:
            sub = el.child("Scale")
            child_attrs["Scale"] = {
                "ScaleX": self._value(sub, "ScaleX", g["sx"], default=1.0),
                "ScaleY": self._value(sub, "ScaleY", g["sy"], default=1.0)}
        if props.get("rotation") is not None:
            node_attrs["RotationSkewX"] = self._value(el, "RotationSkewX", g["rx"], 0.0)
            node_attrs["RotationSkewY"] = self._value(el, "RotationSkewY", g["ry"], 0.0)

        # ---- 非幾何屬性
        if props.get("name") is not None:
            node_attrs["Name"] = str(props["name"])
        if props.get("tag") is not None:
            node_attrs["Tag"] = str(int(props["tag"]))
        if props.get("zOrder") is not None:
            node_attrs["ZOrder"] = str(int(props["zOrder"]))
        if props.get("alpha") is not None:
            node_attrs["Alpha"] = self._value(el, "Alpha", int(props["alpha"]),
                                              default=255, numeric=False)
        if props.get("visible") is not None:
            # Cocos Studio 屬性面板的「可見性」勾選框寫的是 VisibleForFrame——
            # 實測整個專案 2660 個節點用它，Visible 只有 262 個，那是節點樹上的
            # 預覽眼睛。寫錯欄位的話畫面看起來一樣（csdview 兩個都認），
            # 但在編輯器裡勾選框沒動、只有預覽被關掉，設計者看不出這是刻意隱藏的。
            node_attrs["VisibleForFrame"] = self._value(
                el, "VisibleForFrame",
                "True" if _as_bool(props["visible"]) else "False",
                default="True", numeric=False)
            # 兩個欄位不要各說各話：舊檔可能留著 Visible，一併清掉
            node_attrs["Visible"] = None
        for key, attr in (("touchEnabled", "TouchEnable"), ("flipX", "FlipX"),
                          ("flipY", "FlipY"), ("clipAble", "ClipAble")):
            if props.get(key) is not None:
                node_attrs[attr] = "True" if _as_bool(props[key]) else "False"
        if props.get("fontSize") is not None:
            node_attrs["FontSize"] = str(int(props["fontSize"]))

        if props.get("text") is not None:
            attr = TEXT_ATTR.get(ctype)
            if attr is None:
                raise EditError(f"{ref.path} 是 {ctype}，沒有文字內容可以改。")
            node_attrs[attr] = str(props["text"])

        if props.get("color") is not None:
            r, g_, b = _as_color(props["color"])
            child_attrs.setdefault("CColor", {}).update(
                {"R": str(r), "G": str(g_), "B": str(b)})
        if props.get("colorAlpha") is not None:
            child_attrs.setdefault("CColor", {})["A"] = str(int(props["colorAlpha"]))

        for key in ("image", "pressedImage", "disabledImage"):
            if props.get(key) is None:
                continue
            slots = IMAGE_SLOTS[key]
            tag = slots.get(ctype, slots.get("*"))
            path = str(props[key]).replace("\\", "/")
            plist = props.get("plist")
            entry = {"Path": path}
            if plist is not None:
                plist = str(plist).replace("\\", "/")
                entry["Plist"] = plist
                entry["Type"] = "MarkedSubImage" if plist else "Normal"
            elif el.child(tag) is None:
                entry["Type"] = "Normal"
                entry["Plist"] = ""
            child_attrs.setdefault(tag, {}).update(entry)

        if props.get("blendAdditive") is not None:
            if _as_bool(props["blendAdditive"]):
                child_attrs.setdefault("BlendFunc", {}).update({"Src": "1", "Dst": "1"})
            elif el.child("BlendFunc") is not None:
                self.xml.remove(el.child("BlendFunc").route)
                el = self.elem_of(node_path)

        # ---- 逃生門：樣板沒涵蓋的欄位直接寫
        for key, value in (props.get("attrs") or {}).items():
            node_attrs[key] = None if value is None else str(value)
        for tag, values in (props.get("elements") or {}).items():
            child_attrs.setdefault(tag, {}).update(
                {k: (None if v is None else str(v)) for k, v in values.items()})

        # 冗餘版面欄位跟一般屬性合併成同一批：兩邊都會寫到節點的起始標籤上，
        # 分兩次算會各自算出「新屬性插在哪」，同一個插入點就得靠合併去救。
        if moved:
            margin_attrs, pre_attrs = self.layout_fields(node_path, geom=g)
            node_attrs.update(margin_attrs)
            for tag, values in pre_attrs.items():
                child_attrs.setdefault(tag, {}).update(values)

        self._expand_all([node_path])
        cuts = self._collect(node_path, node_attrs, child_attrs)
        touched = 0
        if resized:
            child_cuts, touched = self.reflow_cuts(node_path, (g["w"], g["h"]))
            cuts += child_cuts
        self.xml.splice(cuts)

        after = self.geometry(node_path)
        summary = _diff_summary(before, after)
        if summary:
            self.changes.append(f"{ref.path}: {summary}")
        else:
            self.changes.append(f"{ref.path}: 改了屬性")
        return {
            "node": ref.path,
            "before": _round(before),
            "after": _round(after),
            "childrenReflowed": touched,
            "changed": summary or "屬性",
        }

    # ------------------------------------------------------------ 高階：結構

    def _used_tags(self) -> tuple:
        actions, tags = set(), set()
        for el in self.xml.walk():
            if el.tag not in ("AbstractNodeData", "ObjectData"):
                continue
            if el.get("ActionTag"):
                actions.add(el.get("ActionTag"))
            if el.get("Tag"):
                try:
                    tags.add(int(el.get("Tag")))
                except ValueError:
                    pass
        return actions, tags

    def _fresh_ids(self, count: int) -> list:
        """配出 count 組沒被用過的 (ActionTag, Tag)。

        ActionTag 是時間軸用來認節點的鍵，全檔必須唯一；重複會讓動畫套到錯的節點上。
        """
        actions, tags = self._used_tags()
        next_tag = (max(tags) + 1) if tags else 10
        out = []
        for _ in range(count):
            while True:
                action = str(random.randint(-2_147_483_648, 2_147_483_647))
                if action not in actions:
                    actions.add(action)
                    break
            out.append((action, str(next_tag)))
            next_tag += 1
        return out

    def add_node(self, parent: NodeRef, kind: str, name: str, props: dict) -> dict:
        """在某個父節點底下新增一個節點。"""
        kind = (kind or "node").strip().lower()
        if kind not in NODE_TYPES:
            raise EditError(f"不支援的節點型別 '{kind}'。"
                            f"可用的有：{'、'.join(sorted(NODE_TYPES))}")
        ctype, extra = NODE_TYPES[kind]
        if not name:
            raise EditError("新節點一定要給 name。")

        parent_el = self.elem_of(parent.node_path)
        existing = parent_el.child("Children")
        siblings = existing.children_named("AbstractNodeData") if existing else []
        if any(k.get("Name") == name for k in siblings):
            raise EditError(f"{parent.path} 底下已經有叫 '{name}' 的節點了。")

        (action, tag), = self._fresh_ids(1)
        w = float(props.get("w", 0.0) or 0.0)
        h = float(props.get("h", 0.0) or 0.0)
        ax = float(props.get("anchorX", 0.5) if props.get("anchorX") is not None else 0.5)
        ay = float(props.get("anchorY", 0.5) if props.get("anchorY") is not None else 0.5)
        x = float(props.get("x", 0.0) or 0.0)
        y = float(props.get("y", 0.0) or 0.0)
        pw, ph = (num(parent_el.child("Size").get("X") if parent_el.child("Size") else None),
                  num(parent_el.child("Size").get("Y") if parent_el.child("Size") else None))
        derived = derived_fields(pw, ph, x, y, w, h, ax, ay)

        unknown = [k for k in props
                   if k not in _KNOWN_PROPS and k not in ("attrs", "elements")]
        if unknown:
            raise EditError(
                f"不認得的屬性：{'、'.join(unknown)}。"
                f"可用的有：{'、'.join(sorted(_KNOWN_PROPS))}")

        attrs = {"Name": name, "ActionTag": action, "Tag": tag}
        attrs.update(extra)
        for key in layout.MARGIN_KEYS:
            if abs(derived[key]) > 5e-5:
                attrs[key] = fmt(derived[key])
        attrs["ctype"] = ctype

        children: list = [("Size", {"X": fmt(w), "Y": fmt(h)})]
        children.append(("AnchorPoint", {"ScaleX": fmt(ax), "ScaleY": fmt(ay)}))
        children.append(("Position", {"X": fmt(x), "Y": fmt(y)}))
        children.append(("Scale", {"ScaleX": "1.0000", "ScaleY": "1.0000"}))
        children.append(("CColor", {"A": "255", "R": "255", "G": "255", "B": "255"}))
        pre_pos = {}
        pre_size = {}
        if derived[("PrePosition", "X")] is not None:
            pre_pos["X"] = fmt(derived[("PrePosition", "X")])
        if derived[("PrePosition", "Y")] is not None:
            pre_pos["Y"] = fmt(derived[("PrePosition", "Y")])
        if derived[("PreSize", "X")] is not None:
            pre_size["X"] = fmt(derived[("PreSize", "X")])
        if derived[("PreSize", "Y")] is not None:
            pre_size["Y"] = fmt(derived[("PreSize", "Y")])
        children.append(("PrePosition", pre_pos))
        children.append(("PreSize", pre_size))

        if ctype == "TextObjectData":
            children.append(("OutlineColor", {"A": "255", "R": "255", "G": "0", "B": "0"}))
            children.append(("ShadowColor", {"A": "255", "R": "110", "G": "110", "B": "110"}))
        if ctype == "PanelObjectData":
            children += [("SingleColor", {"A": "255", "R": "150", "G": "200", "B": "255"}),
                         ("FirstColor", {"A": "255", "R": "150", "G": "200", "B": "255"}),
                         ("EndColor", {"A": "255", "R": "255", "G": "255", "B": "255"}),
                         ("ColorVector", {"ScaleY": "1.0000"})]

        if not props.get("image") and ctype == "ProjectNodeObjectData":
            raise EditError("project 型別的節點一定要用 image 指定要嵌入的 .csd 路徑。")

        lines = _fragment_lines("AbstractNodeData", attrs, children, self.xml.newline)
        container_route = self.xml.ensure_child(parent_el.route, "Children", CHILD_ORDER)
        self.xml.insert_fragment(container_route, lines)

        index = len(siblings)
        node_path = parent.node_path + (index,)
        path = f"{parent.path}/{name}"
        new_ref = NodeRef(node_path, name, ctype, path, parent.depth + 1)

        # 幾何欄位已經寫進樣板了，其餘一律交給 set_props——同一套屬性不要實作兩次，
        # 否則樣板這邊漏掉的（color、alpha、visible…）會被靜默忽略，而不是報錯。
        rest = {k: v for k, v in props.items()
                if k not in ("x", "y", "w", "h", "anchorX", "anchorY",
                             "dx", "dy", "preX", "preY", "preW", "preH")
                and v is not None}
        if rest:
            self.set_props(new_ref, rest)
            self.changes.pop()          # set_props 的紀錄併進下面這一行

        self.changes.append(f"{path}: 新增 {kind} 節點")
        return {"node": path, "nodePath": list(node_path),
                "ctype": ctype, "actionTag": action, "tag": tag,
                "propsApplied": sorted(rest)}

    def delete_node(self, ref: NodeRef) -> dict:
        """刪掉一個節點，連同它在時間軸上的動畫。"""
        if not ref.node_path:
            raise EditError("不能刪掉根節點。")
        el = self.elem_of(ref.node_path)
        doomed = {e.get("ActionTag") for e in self.xml.walk(el)
                  if e.tag == "AbstractNodeData" and e.get("ActionTag")}
        descendants = sum(1 for e in self.xml.walk(el) if e.tag == "AbstractNodeData") - 1

        removed_timelines = self._drop_timelines(doomed)
        self.xml.remove(self.elem_of(ref.node_path).route)
        self._drop_empty_children(ref.node_path[:-1])
        self.changes.append(f"{ref.path}: 刪除節點"
                            + (f"（含 {descendants} 個子節點）" if descendants else ""))
        return {"node": ref.path, "descendants": descendants,
                "timelinesRemoved": removed_timelines}

    def _drop_empty_children(self, parent_path: tuple) -> bool:
        """父層的最後一個子節點被拿走時，把空殼 `<Children>` 一起清掉。

        空的 `<Children></Children>` csdview 畫得出來、`doctor` 全綠、執行期也讀得過，
        Cocos Studio 卻會直接解析失敗（檔名變紅、開檔報錯）——只有在編輯器裡打得開
        才驗得出來，所以刪除／搬移的當下就清乾淨，不要留給人工掃描。

        只清真的空掉的那一個：`<Children>` 裡還有任何元素就不動它。
        """
        container = self.elem_of(parent_path).child("Children")
        if container is None or container.children:
            return False
        self.xml.remove(container.route)
        return True

    def _drop_timelines(self, action_tags: set) -> int:
        """把指向這些 ActionTag 的時間軸一次拿掉。

        留著會變成指向不存在節點的孤兒時間軸，Cocos Studio 開檔時會抱怨。
        一條一條刪的話每刪一次就要重建一次索引，動畫多的節點會很慢。
        """
        animation = self.xml.find_first("Animation")
        if animation is None:
            return 0
        doomed = [line.route for line in animation.children_named("Timeline")
                  if line.get("ActionTag") in action_tags]
        return self.xml.remove_many(doomed)

    def duplicate_node(self, ref: NodeRef, name: "str | None",
                       dx: float = 0.0, dy: float = 0.0,
                       with_animation: bool = False) -> dict:
        """複製一個節點（含子樹），配上全新的 ActionTag 與 Tag。

        ActionTag 一定要換：時間軸靠它認節點，沿用原本的會讓兩個節點被同一段動畫驅動。
        """
        if not ref.node_path:
            raise EditError("不能複製根節點。")
        fragment = self.xml.slice_of(self.elem_of(ref.node_path).route)
        inner = Xml(fragment)

        holders = [e for e in inner.walk() if e.tag == "AbstractNodeData"]
        fresh = self._fresh_ids(len(holders))
        mapping: dict = {}
        cuts: list = []
        for el, (action, tag) in zip(holders, fresh):
            old = el.get("ActionTag")
            if old:
                mapping[old] = action
            cuts += inner._attr_cuts(el, {"ActionTag": action, "Tag": tag})
        inner.splice(cuts)

        new_name = name or _unique_name(ref.name, {
            k.get("Name") for k in self.elem_of(ref.node_path[:-1]).child("Children")
            .children_named("AbstractNodeData")})
        inner.set_attrs((), {"Name": new_name})

        parent_path = ref.node_path[:-1]
        indent = self.xml.indent_of(self.elem_of(ref.node_path))
        lines = [line[len(indent):] if line.startswith(indent) else line
                 for line in inner.data.split(self.xml.newline)]
        self.xml.insert_fragment(self.elem_of(parent_path).child("Children").route, lines)

        index = len(self.elem_of(parent_path).child("Children")
                    .children_named("AbstractNodeData")) - 1
        new_path = parent_path + (index,)
        if dx or dy:
            g = self.geometry(new_path)
            self.set_props(
                NodeRef(new_path, new_name, ref.ctype,
                        f"{'/'.join(ref.path.split('/')[:-1])}/{new_name}", ref.depth),
                {"x": g["x"] + dx, "y": g["y"] + dy})

        cloned = self._clone_timelines(mapping) if with_animation else 0
        self.changes.append(f"{ref.path}: 複製成 '{new_name}'")
        return {"source": ref.path, "node": new_name, "nodePath": list(new_path),
                "newActionTags": len(mapping), "timelinesCloned": cloned}

    def _clone_timelines(self, mapping: dict) -> int:
        """把原節點的時間軸複製一份給新的 ActionTag。"""
        if not mapping:
            return 0
        animation = self.xml.find_first("Animation")
        if animation is None:
            return 0
        blocks: list = []
        for line in animation.children_named("Timeline"):
            old = line.get("ActionTag")
            if old in mapping:
                text = self.xml.slice_of(line.route)
                blocks.append(re.sub(rb'ActionTag="' + re.escape(old.encode()) + rb'"',
                                     b'ActionTag="' + escape_attr(mapping[old]) + b'"',
                                     text, count=1))
        if not blocks:
            return 0
        indent = self.xml.indent_of(animation.children[0]) if animation.children else b"  "
        payload = b""
        for block in blocks:
            payload += self.xml.newline + indent + block
        point = animation.children[-1].end if animation.children else animation.tag_end
        self.xml.splice([(point, point, payload)])
        return len(blocks)

    def reparent(self, ref: NodeRef, new_parent: NodeRef,
                 keep: str = "screen") -> dict:
        """把節點搬到另一個父層底下。

        keep="screen" 會重算座標，讓它在畫面上待在原地（預設，也是搬移時通常要的）。
        keep="local" 則保留原本的座標數字，畫面位置會跟著新父層跑。
        """
        if not ref.node_path:
            raise EditError("不能搬動根節點。")
        if new_parent.node_path[:len(ref.node_path)] == ref.node_path:
            raise EditError(f"不能把 {ref.path} 搬進它自己的子樹裡（{new_parent.path}）。")
        if ref.node_path[:-1] == new_parent.node_path:
            return {"node": ref.path, "newParent": new_parent.path, "moved": False,
                    "note": "本來就在這個父層底下，沒有動作。"}

        g = self.geometry(ref.node_path)
        old_space = self.child_space_matrix(ref.node_path[:-1])
        new_space = self.child_space_matrix(new_parent.node_path)
        warning = None
        if keep == "screen":
            world = old_space.apply(g["x"], g["y"])
            try:
                nx, ny = new_space.inverse().apply(*world)
            except ValueError as exc:
                raise EditError(f"{new_parent.path} 的縮放是 0，算不出座標：{exc}")
            old_scale, new_scale = old_space.scale, new_space.scale
            sx = g["sx"] * (old_scale[0] / new_scale[0]) if new_scale[0] else g["sx"]
            sy = g["sy"] * (old_scale[1] / new_scale[1]) if new_scale[1] else g["sy"]
            if abs(old_space.rotation - new_space.rotation) > 0.01:
                warning = ("兩邊的父層旋轉角度不同，只換算了位置與縮放，"
                           "節點自身的旋轉沒有補償，畫面上會歪掉。")
        else:
            nx, ny, sx, sy = g["x"], g["y"], g["sx"], g["sy"]

        fragment = self.xml.slice_of(self.elem_of(ref.node_path).route)
        indent = self.xml.indent_of(self.elem_of(ref.node_path))
        lines = [line[len(indent):] if line.startswith(indent) else line
                 for line in fragment.split(self.xml.newline)]

        target = _shift_after_removal(new_parent.node_path, ref.node_path)
        self.xml.remove(self.elem_of(ref.node_path).route)
        # 搬走最後一個子節點時舊父層會空掉。這裡清掉不會動到 target：
        # 空掉代表舊父層底下已經沒有別的節點，新父層就不可能在它底下。
        self._drop_empty_children(ref.node_path[:-1])

        container_route = self.xml.ensure_child(
            self.elem_of(target).route, "Children", CHILD_ORDER)
        self.xml.insert_fragment(container_route, lines)
        index = len(self.elem_of(target).child("Children")
                    .children_named("AbstractNodeData")) - 1
        new_path = target + (index,)

        moved_ref = NodeRef(new_path, ref.name, ref.ctype,
                            f"{new_parent.path}/{ref.name}", len(new_path))
        self.set_props(moved_ref, {"x": nx, "y": ny, "scaleX": sx, "scaleY": sy})
        self.changes.append(f"{ref.path}: 搬到 {new_parent.path} 底下")
        result = {"node": moved_ref.path, "from": ref.path,
                  "newParent": new_parent.path, "moved": True, "keep": keep,
                  "position": [round(nx, 2), round(ny, 2)],
                  "scale": [round(sx, 4), round(sy, 4)]}
        if warning:
            result["warning"] = warning
        return result

    # ------------------------------------------------------------ 落地

    def dirty(self) -> bool:
        return self.xml.data != self.original

    def check_unchanged(self) -> None:
        """確認檔案自從我們讀進來之後沒有被別人改過。

        對手不只是另一個 agent，更常見的是使用者自己在 Cocos Studio 裡開著同一個檔。
        鎖擋不住編輯器，所以這裡用「比對修改時間與大小」的樂觀檢查：
        偵測到就中止並要求重讀，寧可讓這次編輯失敗，也不要無聲吃掉別人的工作。
        """
        now = _stamp(self.path)
        if now == self.opened_as:
            return
        if now is None:
            raise EditError(f"{os.path.basename(self.path)} 在編輯期間被刪除或改名了，"
                            "這次的改動沒有寫入。")
        # 時間戳對不上不一定代表內容真的變了（被 touch 過、或存成一樣的內容），
        # 這時再實際比一次內容，免得白白擋掉一次正常的編輯
        try:
            with open(self.path, "rb") as fh:
                if fh.read() == self.original:
                    self.opened_as = now
                    return
        except OSError:
            pass
        raise EditError(
            f"{os.path.basename(self.path)} 在編輯期間被別的程式改過"
            "（可能是 Cocos Studio 存檔，或另一個 agent）。"
            "為了不蓋掉對方的改動，這次的編輯已經放棄，原檔沒有動。"
            "請重新讀一次現在的內容再改一遍。")

    def save(self, description: str) -> dict:
        """驗證、備份，然後原子寫入。"""
        if not self.dirty():
            return {"written": False, "note": "內容沒有變化，沒有寫檔。"}
        self.check_unchanged()
        self._validate()
        record = history.snapshot(self.path, description)

        folder = os.path.dirname(self.path)
        handle, temp = tempfile.mkstemp(dir=folder, suffix=".csd.tmp")
        try:
            with os.fdopen(handle, "wb") as fh:
                fh.write(self.xml.data)
            # 備份與驗證都要花時間，換檔之前再確認一次，把空窗縮到最小。
            # 第二次檢查與 os.replace 之間仍有一道極短的窗：Cocos Studio 不理會
            # 檔案鎖，.csd 旁邊也不能放 .lock（會污染工作區與 git status），
            # 所以這道窗收不掉，只能靠樂觀檢查把它縮到最短。
            self.check_unchanged()
            locking.replace_with_retry(temp, self.path)
        except locking.ReplaceBusy as exc:
            if os.path.exists(temp):
                os.remove(temp)
            # 換檔是這條路徑的最後一步，失敗就代表原檔完全沒被碰到。
            # 這件事只有這裡知道，補進訊息裡模型才知道可以放心重試整批編輯。
            raise EditError(f"{exc} 這次的改動沒有寫入，原檔沒有動。") from exc
        except BaseException:
            if os.path.exists(temp):
                os.remove(temp)
            raise
        self.original = self.xml.data
        self.opened_as = _stamp(self.path)
        history.mark_head(self.path, self.xml.data)
        return {"written": True, "file": self.path, "snapshot": record["serial"],
                "bytes": len(self.xml.data), "changes": list(self.changes)}

    def _validate(self) -> None:
        """確認改完的內容 csdview 還讀得動——寧可現在失敗，也不要寫出壞檔。"""
        from csdview import parser as csd_parser

        try:
            csd_parser.parse_bytes(self.xml.data, self.path)
        except Exception as exc:
            raise EditError(f"改完的內容無法解析，已經放棄寫檔（原檔沒有動）：{exc}")


# ---------------------------------------------------------------- 建立新檔

# 根節點的兩種型別。Layer 有固定畫布（設計解析度），Node 沒有、尺寸由內容決定。
ROOT_TYPES = {
    "layer": ("Layer", "GameLayerObjectData", "Layer"),
    "node": ("Node", "GameNodeObjectData", "Node"),
}


def create(path: str, root_type: str = "node", width: float = 0.0,
           height: float = 0.0, root_name: "str | None" = None,
           version: str = "3.10.0.0", overwrite: bool = False) -> dict:
    """從零建立一個 .csd。

    產出的格式跟 Cocos Studio 存出來的完全一樣：CRLF、沒有 BOM、沒有 XML 宣告、
    結尾不留換行。差一樣編輯器下次存檔就會把整份檔案重排，git 上變成一大包 diff。

    只寫檔案本身；要讓 Cocos Studio 的資源列表看得到它，還得登錄進 .ccs（見 ccs.register）。
    """
    import uuid

    kind = (root_type or "node").strip().lower()
    if kind not in ROOT_TYPES:
        raise EditError(f"根節點型別只能是 {'、'.join(ROOT_TYPES)}，收到 '{root_type}'。")
    type_name, ctype, default_name = ROOT_TYPES[kind]

    path = os.path.abspath(path)
    if not path.lower().endswith(".csd"):
        raise EditError(f"檔名要以 .csd 結尾：{path}")

    stem = os.path.splitext(os.path.basename(path))[0]
    name = root_name or default_name
    lines = [
        b"<GameFile>",
        b'  <PropertyGroup Name="' + escape_attr(stem) + b'" Type="'
        + escape_attr(type_name) + b'" ID="' + escape_attr(str(uuid.uuid4()))
        + b'" Version="' + escape_attr(version) + b'" />',
        b'  <Content ctype="GameProjectContent">',
        b"    <Content>",
        b'      <Animation Duration="0" Speed="1.0000" />',
        b'      <ObjectData Name="' + escape_attr(name) + b'" Tag="'
        + escape_attr(random.randint(2, 9999)) + b'" ctype="'
        + escape_attr(ctype) + b'">',
        b'        <Size X="' + escape_attr(fmt(width)) + b'" Y="'
        + escape_attr(fmt(height)) + b'" />',
        b"      </ObjectData>",
        b"    </Content>",
        b"  </Content>",
        b"</GameFile>",
    ]
    data = b"\r\n".join(lines)

    Xml(data)                       # 生出來的東西自己先驗一遍
    folder = os.path.dirname(path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder, exist_ok=True)

    if overwrite:
        handle, temp = tempfile.mkstemp(dir=folder or ".", suffix=".csd.tmp")
        try:
            with os.fdopen(handle, "wb") as fh:
                fh.write(data)
            locking.replace_with_retry(temp, path)
        except locking.ReplaceBusy as exc:
            if os.path.exists(temp):
                os.remove(temp)
            raise EditError(f"{exc} 這個檔沒有被覆蓋，原本的內容還在。") from exc
        except BaseException:
            if os.path.exists(temp):
                os.remove(temp)
            raise
    else:
        # 「不存在才建立」交給檔案系統一次做完。先 os.path.exists 再寫的話，
        # 兩個並行的建檔都會通過檢查，後者無聲蓋掉前者。
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
        except FileExistsError:
            raise EditError(
                f"檔案已經存在：{path}。要覆蓋請把 overwrite 設為 true。") from None
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)

    return {"file": path, "rootType": type_name, "rootName": name,
            "ctype": ctype, "size": [width, height], "bytes": len(data)}


# ---------------------------------------------------------------- 小工具

class _Skip:
    def __repr__(self):
        return "<不要動>"


_SKIP = _Skip()

_KNOWN_PROPS = {
    "x", "y", "w", "h", "dx", "dy", "anchorX", "anchorY", "scaleX", "scaleY",
    "rotation", "preX", "preY", "preW", "preH", "name", "tag", "zOrder",
    "alpha", "visible", "color", "colorAlpha", "text", "fontSize",
    "image", "pressedImage", "disabledImage", "plist",
    "touchEnabled", "flipX", "flipY", "clipAble", "blendAdditive",
}


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _as_color(value) -> tuple:
    """接受 '#RRGGBB'、'RRGGBB' 或 [r, g, b]。"""
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return tuple(int(v) for v in value[:3])
    text = str(value).strip().lstrip("#")
    if len(text) == 6:
        try:
            return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
        except ValueError:
            pass
    raise EditError(f"看不懂的顏色 '{value}'，請用 '#RRGGBB' 或 [r, g, b]。")


def _fragment_lines(tag: str, attrs: dict, children: list, newline: bytes) -> list:
    """把一個節點序列化成一行行的 XML（不含縮排，插入時再補）。"""
    head = b"<" + tag.encode()
    for key, value in attrs.items():
        head += b" " + key.encode() + b'="' + escape_attr(value) + b'"'
    if not children:
        return [head + b" />"]
    lines = [head + b">"]
    for name, values in children:
        line = b"  <" + name.encode()
        for key, value in values.items():
            line += b" " + key.encode() + b'="' + escape_attr(value) + b'"'
        lines.append(line + b" />")
    lines.append(b"</" + tag.encode() + b">")
    return lines


def _stamp(path: str):
    """檔案的「有沒有被動過」指紋：修改時間加大小。抓不到檔案就回 None。"""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def _strip_index(path: str) -> str:
    """拿掉路徑上的 `[n]` 同名序號。"""
    return re.sub(r"\[\d+\](?=/|$)", "", path)


def _unique_name(base: str, taken: set) -> str:
    stem = re.sub(r"_\d+$", "", base or "Node")
    index = 1
    while True:
        candidate = f"{stem}_{index}"
        if candidate not in taken:
            return candidate
        index += 1


def _shift_after_removal(target: tuple, removed: tuple) -> tuple:
    """某個節點被移除後，另一個節點的 node_path 會不會往前挪。

    只有「跟被移除節點同一個父層、而且排在它後面」的那一段索引要減一。
    """
    level = len(removed) - 1
    if len(target) > level and target[:level] == removed[:level] and target[level] > removed[level]:
        shifted = list(target)
        shifted[level] -= 1
        return tuple(shifted)
    return target


def _round(g: dict) -> dict:
    return {k: round(v, 3) for k, v in g.items()}


def _diff_summary(before: dict, after: dict) -> str:
    labels = {"x": "x", "y": "y", "w": "寬", "h": "高", "ax": "錨點x", "ay": "錨點y",
              "sx": "縮放x", "sy": "縮放y", "rx": "旋轉x", "ry": "旋轉y"}
    parts = [f"{labels[k]} {before[k]:g} -> {after[k]:g}"
             for k in labels if abs(after[k] - before[k]) > 1e-6]
    return "、".join(parts)
