"""Cocos Studio 冗餘版面欄位的換算，以及節點的世界變換。

.csd 把同一件事存了三份：絕對值（Position / Size）、對父層的百分比
（PrePosition / PreSize）、以及四個邊界（LeftMargin / RightMargin /
TopMargin / BottomMargin）。編輯器每次存檔都讓三者一致。

只改其中一份會做出「版面過期」的檔案——csdview 的 `doctor` 會把它報成
`staleLayout`，移植到引擎後位置也會跟編輯器裡看到的不一樣。
所以這裡集中處理換算，任何改到位置或尺寸的操作都必須回頭呼叫 `derived_fields`。

公式是拿專案裡的真實檔案反推並驗證過的，其中兩點容易寫錯：

- 邊界用的是**未套用 Scale 的尺寸**。實測 Fx_Pbar1（Scale 0.59、Size 545）
  存的 LeftMargin 是 -272.5 = 0 - 0.5 x 545，不是 -160.775。
- 父層某一軸的尺寸是 0 時，該軸的百分比欄位無從計算，編輯器直接不寫。
"""
from __future__ import annotations

import math

# Cocos Studio 存檔時子元素的順序。功能上無所謂（cocos 與 csdview 都用 tag 名稱查找），
# 但照著排，產生的檔案才跟編輯器存出來的長得一樣，diff 也才好審。
CHILD_ORDER = [
    "Size", "Children", "AnchorPoint", "Position", "Scale", "CColor",
    "PrePosition", "PreSize",
    "TextColor", "OutlineColor", "ShadowColor",
    "FileData", "NormalFileData", "PressedFileData", "DisabledFileData",
    "NormalBackFileData", "PressedBackFileData", "DisableBackFileData",
    "NodeNormalFileData", "NodeDisableFileData",
    "LabelBMFontFile_CNB", "FontResource",
    "SingleColor", "FirstColor", "EndColor", "ColorVector",
    "BlendFunc",
]

# 屬性省略時的預設值。編輯器把等於預設值的屬性整個不寫（<AnchorPoint /> 就是這樣來的），
# 我們跟著這個規則，改回預設值時就刪掉屬性，diff 才不會多出一堆 ="0.0000"。
DEFAULTS = {
    ("AnchorPoint", "ScaleX"): 0.0, ("AnchorPoint", "ScaleY"): 0.0,
    ("Position", "X"): 0.0, ("Position", "Y"): 0.0,
    ("Scale", "ScaleX"): 1.0, ("Scale", "ScaleY"): 1.0,
    ("PrePosition", "X"): 0.0, ("PrePosition", "Y"): 0.0,
    ("PreSize", "X"): 0.0, ("PreSize", "Y"): 0.0,
}

MARGIN_KEYS = ("LeftMargin", "RightMargin", "TopMargin", "BottomMargin")

# 各元素上屬性的慣用順序。新增一個原本省略掉的屬性時照這個順序插入，
# 否則會寫成 <Position Y="-40" X="60" />——功能沒差，但一看就知道不是編輯器存的。
# <AbstractNodeData> 的屬性太多也太雜，不列表；那裡只靠「ctype 一律排最後」這條規則。
_XY = ["X", "Y"]
_SCALE = ["ScaleX", "ScaleY"]
_ARGB = ["A", "R", "G", "B"]
_FILE = ["Type", "Path", "Plist"]

ATTR_ORDER = {
    "Size": _XY, "Position": _XY, "PrePosition": _XY, "PreSize": _XY,
    "AnchorPoint": _SCALE, "Scale": _SCALE, "ColorVector": _SCALE,
    "CColor": _ARGB, "SingleColor": _ARGB, "FirstColor": _ARGB, "EndColor": _ARGB,
    "TextColor": _ARGB, "OutlineColor": _ARGB, "ShadowColor": _ARGB,
    "FileData": _FILE, "NormalFileData": _FILE, "PressedFileData": _FILE,
    "DisabledFileData": _FILE, "NormalBackFileData": _FILE,
    "PressedBackFileData": _FILE, "DisableBackFileData": _FILE,
    "NodeNormalFileData": _FILE, "NodeDisableFileData": _FILE,
    "LabelBMFontFile_CNB": _FILE, "FontResource": _FILE,
    "BlendFunc": ["Src", "Dst"],
}


def fmt(value: float) -> str:
    """照 Cocos Studio 的格式寫浮點數。"""
    text = f"{float(value):.4f}"
    return "0.0000" if text == "-0.0000" else text


def num(value, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# 「這一軸用百分比擺位／定尺寸」的開關。百分比失去意義時要連開關一起撤掉，
# 不然檔案就變成「請照百分比擺」而百分比是 0——引擎會把節點放到 0 的位置。
# 兩種拼法都要處理：Cocos Studio 對尺寸同時寫 Enable 與 Enabled 兩個屬性。
PERCENT_FLAGS = {
    ("PrePosition", "X"): ("PositionPercentXEnabled",),
    ("PrePosition", "Y"): ("PositionPercentYEnabled",),
    ("PreSize", "X"): ("PercentWidthEnable", "PercentWidthEnabled"),
    ("PreSize", "Y"): ("PercentHeightEnable", "PercentHeightEnabled"),
}


def derived_fields(parent_w: float, parent_h: float, x: float, y: float,
                   w: float, h: float, ax: float, ay: float) -> dict:
    """由絕對座標算出四個邊界與百分比欄位。

    回傳的 key 直接對應 XML：邊界是節點自己的屬性，
    PrePosition/PreSize 是子元素，用 ("PrePosition", "X") 這種形式表示。
    父層該軸是 0 時，對應的百分比值回傳 None，代表「這一軸的百分比沒有意義」——
    呼叫端要把該屬性連同 PERCENT_FLAGS 裡的開關一起移除，不能留著舊值。
    """
    return {
        "LeftMargin": x - ax * w,
        "RightMargin": parent_w - (x + (1.0 - ax) * w),
        "BottomMargin": y - ay * h,
        "TopMargin": parent_h - (y + (1.0 - ay) * h),
        ("PrePosition", "X"): (x / parent_w) if parent_w else None,
        ("PrePosition", "Y"): (y / parent_h) if parent_h else None,
        ("PreSize", "X"): (w / parent_w) if parent_w else None,
        ("PreSize", "Y"): (h / parent_h) if parent_h else None,
    }


# ---------------------------------------------------------------- 2D 仿射變換

class Mat:
    """2D 仿射矩陣 [[a, c, tx], [b, d, ty]]，用來換算跨階層的座標。

    座標系跟 cocos 一致：原點左下、Y 軸向上。
    """

    __slots__ = ("a", "b", "c", "d", "tx", "ty")

    def __init__(self, a=1.0, b=0.0, c=0.0, d=1.0, tx=0.0, ty=0.0):
        self.a, self.b, self.c, self.d, self.tx, self.ty = a, b, c, d, tx, ty

    def mul(self, o: "Mat") -> "Mat":
        """self * o：先套用 o，再套用 self。"""
        return Mat(
            self.a * o.a + self.c * o.b,
            self.b * o.a + self.d * o.b,
            self.a * o.c + self.c * o.d,
            self.b * o.c + self.d * o.d,
            self.a * o.tx + self.c * o.ty + self.tx,
            self.b * o.tx + self.d * o.ty + self.ty,
        )

    def apply(self, x: float, y: float) -> tuple:
        return (self.a * x + self.c * y + self.tx,
                self.b * x + self.d * y + self.ty)

    def inverse(self) -> "Mat":
        det = self.a * self.d - self.b * self.c
        if abs(det) < 1e-12:
            raise ValueError("矩陣不可逆（父層被縮放成 0）")
        ia, ib = self.d / det, -self.b / det
        ic, id_ = -self.c / det, self.a / det
        return Mat(ia, ib, ic, id_,
                   -(ia * self.tx + ic * self.ty),
                   -(ib * self.tx + id_ * self.ty))

    @property
    def scale(self) -> tuple:
        return (math.hypot(self.a, self.b), math.hypot(self.c, self.d))

    @property
    def rotation(self) -> float:
        return math.degrees(math.atan2(self.b, self.a))


def child_space(x: float, y: float, ax: float, ay: float, w: float, h: float,
                sx: float, sy: float, rot_x: float, rot_y: float) -> Mat:
    """回傳「這個節點的子座標系 -> 這個節點所在的座標系」的矩陣。

    cocos 的組成順序是 T(position) * R(rotation) * S(scale) * T(-anchor * size)：
    子節點的座標以父節點的**左下角**為原點，而 Position 指的是節點的錨點落在哪裡，
    所以最後要再平移回左下角。
    """
    rx, ry = math.radians(rot_x), math.radians(rot_y)
    # cocos 的 RotationSkewX/Y 是兩軸各自的傾斜角，兩者相等時就是一般的旋轉
    rot = Mat(math.cos(ry), math.sin(ry), -math.sin(rx), math.cos(rx), 0.0, 0.0)
    scale = Mat(sx, 0.0, 0.0, sy, 0.0, 0.0)
    return Mat(tx=x, ty=y).mul(rot).mul(scale).mul(Mat(tx=-ax * w, ty=-ay * h))
