"""時間軸的讀寫。

Cocos Studio 的動畫是「屬性軌道」：每條 <Timeline> 綁一個 ActionTag 與一個屬性，
裡面是關鍵影格。節點靜態屬性是第 0 格的底，時間軸是覆蓋層。

這一層負責把模型友善的 {frame, x, y, easing} 寫成跟編輯器存出來一樣的 XML，
並在寫完後把 <Animation Duration> 重算成所有關鍵影格的最大值。
"""
from __future__ import annotations

from .editor import EditError, _as_bool, _as_color
from .layout import fmt, num
from .xmldoc import escape_attr

# 對外名稱 -> (XML Property, 影格 tag, 預設不內插)
# 不內插的那些（貼圖、混合、開關、內嵌動畫）Cocos 會明確寫 Tween="False"
_PROPS = {
    "position":        ("Position", "PointFrame", False),
    "scale":           ("Scale", "ScaleFrame", False),
    "rotation":        ("RotationSkew", "ScaleFrame", False),
    "anchor":          ("AnchorPoint", "ScaleFrame", False),
    "alpha":           ("Alpha", "IntFrame", False),
    "color":           ("CColor", "ColorFrame", False),
    "visible":         ("VisibleForFrame", "BoolFrame", True),
    "image":           ("FileData", "TextureFrame", True),
    "blend":           ("BlendFunc", "BlendFuncFrame", True),
    "event":           ("FrameEvent", "EventFrame", True),
    "inner":           ("ActionValue", "InnerActionFrame", True),
}

# 也接受 XML 原名與常見別名
_ALIASES = {
    "pos": "position", "xy": "position",
    "rotationskew": "rotation", "rotate": "rotation",
    "anchorpoint": "anchor",
    "ccolor": "color",
    "visibleforframe": "visible",
    "file": "image", "filedata": "image",
    "blendfunc": "blend",
    "frameevent": "event",
    "actionvalue": "inner", "inneraction": "inner",
}
for _name, (_xml, _tag, _hold) in _PROPS.items():
    _ALIASES[_xml.lower()] = _name
    _ALIASES[_name] = _name

# cocos2d-x TweenType 順序，與 csdview.easing.NAMES 一致
_EASING = [
    "Linear",
    "Sine_EaseIn", "Sine_EaseOut", "Sine_EaseInOut",
    "Quad_EaseIn", "Quad_EaseOut", "Quad_EaseInOut",
    "Cubic_EaseIn", "Cubic_EaseOut", "Cubic_EaseInOut",
    "Quart_EaseIn", "Quart_EaseOut", "Quart_EaseInOut",
    "Quint_EaseIn", "Quint_EaseOut", "Quint_EaseInOut",
    "Expo_EaseIn", "Expo_EaseOut", "Expo_EaseInOut",
    "Circ_EaseIn", "Circ_EaseOut", "Circ_EaseInOut",
    "Elastic_EaseIn", "Elastic_EaseOut", "Elastic_EaseInOut",
    "Back_EaseIn", "Back_EaseOut", "Back_EaseInOut",
    "Bounce_EaseIn", "Bounce_EaseOut", "Bounce_EaseInOut",
]
_EASING_LOOKUP = {}
for _i, _n in enumerate(_EASING):
    _EASING_LOOKUP[_n.lower()] = _i
    _EASING_LOOKUP[_n.lower().replace("_", "")] = _i
    _EASING_LOOKUP[_n.lower().replace("_ease", "")] = _i
_EASING_LOOKUP["linear"] = 0
_EASING_LOOKUP["custom"] = -1


def resolve_prop(name: str) -> str:
    key = (name or "").strip().lower().replace("_", "").replace("-", "")
    if key in _ALIASES:
        return _ALIASES[key]
    listed = "、".join(_PROPS)
    raise EditError(f"不認得的動畫屬性 '{name}'。可用的有：{listed}。")


def set_track(csd, ref, prop: str, frames: list) -> dict:
    """覆寫一個節點某一條屬性軌道。frames 是空的就刪掉這條軌道。"""
    kind = resolve_prop(prop)
    xml_prop, frame_tag, hold = _PROPS[kind]
    action = _action_tag(csd, ref)
    parsed = _parse_frames(csd, ref, kind, frames or [])

    anim = _animation(csd)
    removed = _drop_track(csd, anim, action, xml_prop)
    if not parsed:
        _sync_duration(csd)
        csd.changes.append(f"{ref.path}: 刪掉 {kind} 時間軸")
        return {"node": ref.path, "prop": kind, "frames": 0, "removed": removed}

    lines = _timeline_lines(action, xml_prop, frame_tag, hold, kind, parsed)
    _insert_track(csd, action, lines)
    _sync_duration(csd)
    last = parsed[-1]["frame"]
    csd.changes.append(f"{ref.path}: {kind} 時間軸 {len(parsed)} 格（{parsed[0]['frame']}~{last}）")
    return {"node": ref.path, "prop": kind, "xmlProp": xml_prop,
            "actionTag": action, "frames": len(parsed),
            "first": parsed[0]["frame"], "last": last, "replaced": removed}


def set_tracks(csd, ref, tracks: dict) -> list:
    """一次寫同一個節點的多條軌道。"""
    if not isinstance(tracks, dict) or not tracks:
        raise EditError("tracks 要是 {{屬性: 影格清單}} 的物件。")
    return [set_track(csd, ref, prop, frames) for prop, frames in tracks.items()]


def list_tracks(csd, ref=None) -> dict:
    """讀出時間軸。給了 node 就只回那一個節點的。"""
    anim = _animation(csd)
    by_tag = {}
    for node in csd.nodes():
        tag = csd.elem_of(node.node_path).get("ActionTag")
        if tag:
            by_tag.setdefault(tag, []).append(node.path)

    wanted = None
    if ref is not None:
        wanted = _action_tag(csd, ref)

    tracks = []
    for line in anim.children_named("Timeline"):
        tag = line.get("ActionTag") or ""
        if wanted is not None and tag != wanted:
            continue
        xml_prop = line.get("Property") or ""
        kind = _ALIASES.get(xml_prop.lower(), xml_prop)
        frames = [_read_frame(el, kind) for el in line.children]
        paths = by_tag.get(tag) or []
        tracks.append({
            "node": paths[0] if len(paths) == 1 else (paths or None),
            "actionTag": tag,
            "prop": kind,
            "xmlProp": xml_prop,
            "frames": frames,
        })

    clips = []
    listing = csd.xml.find_first("AnimationList")
    if listing is not None:
        for info in listing.children_named("AnimationInfo"):
            clips.append({
                "name": info.get("Name") or "",
                "start": int(num(info.get("StartIndex"))),
                "end": int(num(info.get("EndIndex"))),
            })

    return {
        "duration": int(num(anim.get("Duration"))),
        "speed": num(anim.get("Speed"), 1.0),
        "clips": clips,
        "tracks": tracks,
        "count": len(tracks),
    }


def set_clips(csd, clips: list) -> dict:
    """覆寫 <AnimationList>。空清單就整段拿掉。"""
    existing = csd.xml.find_first("AnimationList")
    if existing is not None:
        csd.xml.remove(existing.route)

    if not clips:
        csd.changes.append("刪掉動畫片段清單")
        return {"clips": []}

    parsed = []
    for item in clips:
        if not isinstance(item, dict) or not item.get("name"):
            raise EditError("每個 clip 都要有 name。")
        start = int(item.get("start", item.get("StartIndex", 0)) or 0)
        end = int(item.get("end", item.get("EndIndex", start)) or start)
        if start < 0 or end < start:
            raise EditError(f"clip '{item['name']}' 的 start/end 不合法（{start}~{end}）。")
        parsed.append((str(item["name"]), start, end))

    lines = [b"<AnimationList>"]
    for name, start, end in parsed:
        lines.append(
            b'  <AnimationInfo Name="' + escape_attr(name)
            + b'" StartIndex="' + escape_attr(start)
            + b'" EndIndex="' + escape_attr(end) + b'">')
        lines.append(b'    <RenderColor A="255" R="255" G="255" B="255" />')
        lines.append(b"  </AnimationInfo>")
    lines.append(b"</AnimationList>")

    anim = _animation(csd)
    # 跟 Animation 同層、緊接在它後面
    indent = csd.xml.indent_of(anim)
    body = csd.xml.newline.join(indent + line for line in lines)
    csd.xml.splice([(anim.end, anim.end, csd.xml.newline + body)])
    _sync_duration(csd, minimum=max(end for _n, _s, end in parsed))
    csd.changes.append("動畫片段：" + "、".join(n for n, _s, _e in parsed))
    return {"clips": [{"name": n, "start": s, "end": e} for n, s, e in parsed]}


def set_speed(csd, speed: float) -> None:
    anim = _animation(csd)
    csd.xml.set_attrs(anim.route, {"Speed": fmt(speed)})
    csd.changes.append(f"動畫速度 {fmt(speed)}")


def set_duration(csd, duration: int) -> dict:
    """把 Duration 設成指定值；不能比最晚的關鍵影格還短。"""
    duration = int(duration)
    if duration < 0:
        raise EditError("duration 不能是負的。")
    anim = _animation(csd)
    highest = 0
    for line in anim.children_named("Timeline"):
        for frame in line.children:
            highest = max(highest, int(num(frame.get("FrameIndex"))))
    if duration < highest:
        raise EditError(
            f"duration 是 {duration}，但最晚的關鍵影格在第 {highest} 格。"
            "把 duration 加大，或先拿掉比較晚的影格。")
    csd.xml.set_attrs(anim.route, {"Duration": str(duration)})
    csd.changes.append(f"動畫長度 {duration} 格")
    return {"duration": duration}


# ---------------------------------------------------------------- 內部

def _animation(csd):
    el = csd.xml.find_first("Animation")
    if el is None:
        raise EditError("這個檔案沒有 <Animation>，不像是完整的 .csd。")
    return el


def _action_tag(csd, ref) -> str:
    if not ref.node_path:
        raise EditError("根節點沒有 ActionTag，不能掛時間軸。")
    tag = csd.elem_of(ref.node_path).get("ActionTag")
    if not tag:
        raise EditError(f"{ref.path} 沒有 ActionTag，無法寫動畫。")
    return tag


def _drop_track(csd, anim, action: str, xml_prop: str) -> bool:
    for line in anim.children_named("Timeline"):
        if line.get("ActionTag") == action and line.get("Property") == xml_prop:
            csd.xml.remove(line.route)
            return True
    return False


def _insert_track(csd, action: str, lines: list) -> None:
    anim = _animation(csd)
    # 同一 ActionTag 的軌道挨在一起，跟編輯器存檔的習慣一樣
    last_same = None
    for line in anim.children_named("Timeline"):
        if line.get("ActionTag") == action:
            last_same = line
    if last_same is not None:
        indent = csd.xml.indent_of(anim) + b"  "
        body = csd.xml.newline.join(indent + line for line in lines)
        csd.xml.splice([(last_same.end, last_same.end, csd.xml.newline + body)])
        return
    csd.xml.insert_fragment(anim.route, lines)


def _sync_duration(csd, minimum: int = 0) -> int:
    anim = _animation(csd)
    highest = 0
    for line in anim.children_named("Timeline"):
        for frame in line.children:
            highest = max(highest, int(num(frame.get("FrameIndex"))))
    duration = max(highest, int(minimum or 0))
    csd.xml.set_attrs(anim.route, {"Duration": str(duration)})
    return duration


def _parse_frames(csd, ref, kind: str, frames: list) -> list:
    if not isinstance(frames, list):
        raise EditError("frames 要是陣列，每個元素是一格關鍵影格。")
    prev = _seed(csd, ref, kind)
    out = []
    seen = set()
    for index, raw in enumerate(frames):
        if not isinstance(raw, dict):
            raise EditError(f"第 {index + 1} 格不是物件。")
        if raw.get("frame") is None and raw.get("FrameIndex") is None:
            raise EditError(f"第 {index + 1} 格沒有 frame。")
        frame = int(raw.get("frame", raw.get("FrameIndex")))
        if frame < 0:
            raise EditError(f"frame 不能是負的（收到 {frame}）。")
        if frame in seen:
            raise EditError(f"frame {frame} 出現兩次。同一格不要寫兩筆，把值合在一起。")
        seen.add(frame)
        item = _fill_frame(kind, raw, prev)
        item["frame"] = frame
        out.append(item)
        prev = item
    out.sort(key=lambda k: k["frame"])
    return out


def _seed(csd, ref, kind: str) -> dict:
    """第一格省略的欄位從節點目前的靜態值補。"""
    el = csd.elem_of(ref.node_path)
    g = csd.geometry(ref.node_path)
    color_el = el.child("CColor")
    file_el = el.child("FileData")
    blend_el = el.child("BlendFunc")
    vis = el.get("VisibleForFrame")
    if vis is None:
        vis = el.get("Visible")
    return {
        "x": g["x"], "y": g["y"],
        "scaleX": g["sx"], "scaleY": g["sy"],
        "rotationX": g["rx"], "rotationY": g["ry"],
        "anchorX": g["ax"], "anchorY": g["ay"],
        "alpha": int(num(el.get("Alpha"), 255)),
        "color": (
            int(num(color_el.get("R") if color_el else None, 255)),
            int(num(color_el.get("G") if color_el else None, 255)),
            int(num(color_el.get("B") if color_el else None, 255)),
        ),
        "colorAlpha": int(num(color_el.get("A") if color_el else None, 255)),
        "visible": vis is None or _as_bool(vis),
        "image": (file_el.get("Path") if file_el else "") or "",
        "plist": (file_el.get("Plist") if file_el else "") or "",
        "src": int(num(blend_el.get("Src") if blend_el else None, 770)),
        "dst": int(num(blend_el.get("Dst") if blend_el else None, 771)),
        "event": "",
        "innerType": "SingleFrame",
        "animation": "-- ALL --",
        "innerFrame": 0,
        "easing": 0,
        "tween": None,
    }


def _fill_frame(kind: str, raw: dict, prev: dict) -> dict:
    item = dict(prev)
    item["easing"] = _easing_of(raw) if "easing" in raw or "Easing" in raw else prev.get("easing", 0)
    if raw.get("easingPoints") is not None:
        item["easingPoints"] = raw["easingPoints"]
    elif "easingPoints" in item and "easing" in raw:
        item.pop("easingPoints", None)
    if raw.get("tween") is not None:
        item["tween"] = _as_bool(raw["tween"])
    else:
        item["tween"] = prev.get("tween")

    if kind == "position":
        if raw.get("x") is not None:
            item["x"] = float(raw["x"])
        if raw.get("y") is not None:
            item["y"] = float(raw["y"])
        if isinstance(raw.get("value"), (list, tuple)) and len(raw["value"]) >= 2:
            item["x"], item["y"] = float(raw["value"][0]), float(raw["value"][1])
    elif kind == "scale":
        if raw.get("scale") is not None:
            item["scaleX"] = item["scaleY"] = float(raw["scale"])
        if raw.get("scaleX") is not None or raw.get("x") is not None:
            item["scaleX"] = float(raw.get("scaleX", raw.get("x")))
        if raw.get("scaleY") is not None or raw.get("y") is not None:
            item["scaleY"] = float(raw.get("scaleY", raw.get("y")))
    elif kind == "rotation":
        if raw.get("rotation") is not None:
            item["rotationX"] = item["rotationY"] = float(raw["rotation"])
        if raw.get("rotationX") is not None or raw.get("x") is not None:
            item["rotationX"] = float(raw.get("rotationX", raw.get("x")))
        if raw.get("rotationY") is not None or raw.get("y") is not None:
            item["rotationY"] = float(raw.get("rotationY", raw.get("y")))
    elif kind == "anchor":
        if raw.get("anchorX") is not None or raw.get("x") is not None:
            item["anchorX"] = float(raw.get("anchorX", raw.get("x")))
        if raw.get("anchorY") is not None or raw.get("y") is not None:
            item["anchorY"] = float(raw.get("anchorY", raw.get("y")))
    elif kind == "alpha":
        if raw.get("alpha") is not None or raw.get("value") is not None:
            item["alpha"] = int(raw.get("alpha", raw.get("value")))
    elif kind == "color":
        if raw.get("color") is not None:
            item["color"] = _as_color(raw["color"])
        if raw.get("alpha") is not None or raw.get("colorAlpha") is not None:
            item["colorAlpha"] = int(raw.get("alpha", raw.get("colorAlpha")))
    elif kind == "visible":
        if raw.get("visible") is not None or raw.get("value") is not None:
            item["visible"] = _as_bool(raw.get("visible", raw.get("value")))
    elif kind == "image":
        if raw.get("image") is not None:
            item["image"] = str(raw["image"]).replace("\\", "/")
        if "plist" in raw:
            item["plist"] = str(raw["plist"] or "").replace("\\", "/")
    elif kind == "blend":
        if raw.get("blendAdditive") is not None and _as_bool(raw["blendAdditive"]):
            item["src"], item["dst"] = 1, 1
        if raw.get("src") is not None:
            item["src"] = int(raw["src"])
        if raw.get("dst") is not None:
            item["dst"] = int(raw["dst"])
    elif kind == "event":
        item["event"] = str(raw.get("event", raw.get("value", "")))
    elif kind == "inner":
        if raw.get("innerType") is not None or raw.get("type") is not None:
            item["innerType"] = str(raw.get("innerType", raw.get("type")))
        if raw.get("animation") is not None:
            item["animation"] = str(raw["animation"])
        if raw.get("innerFrame") is not None or raw.get("value") is not None:
            item["innerFrame"] = int(raw.get("innerFrame", raw.get("value")))
    return item


def _easing_of(raw: dict) -> int:
    value = raw.get("easing", raw.get("Easing", 0))
    if isinstance(value, bool):
        raise EditError("easing 請給數字或名稱（例如 5、quadOut），不要給 true/false。")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    key = str(value).strip().lower().replace("-", "").replace(" ", "")
    if key in _EASING_LOOKUP:
        return _EASING_LOOKUP[key]
    raise EditError(
        f"不認得的 easing '{value}'。給 0~29 的數字，或 Linear / Quad_EaseOut 這種名稱。")


def _timeline_lines(action: str, xml_prop: str, frame_tag: str,
                    hold: bool, kind: str, frames: list) -> list:
    children = [_frame_elem(kind, frame_tag, hold, kf) for kf in frames]
    return _xml_lines("Timeline", {"ActionTag": action, "Property": xml_prop}, children)


def _frame_elem(kind: str, tag: str, hold: bool, kf: dict) -> tuple:
    tween = kf.get("tween")
    if tween is None:
        tween = not hold
    attrs = {"FrameIndex": str(kf["frame"])}
    kids = []

    if tag == "PointFrame":
        attrs["X"] = fmt(kf["x"])
        attrs["Y"] = fmt(kf["y"])
    elif tag == "ScaleFrame":
        if kind == "scale":
            attrs["X"] = fmt(kf["scaleX"])
            attrs["Y"] = fmt(kf["scaleY"])
        elif kind == "rotation":
            attrs["X"] = fmt(kf["rotationX"])
            attrs["Y"] = fmt(kf["rotationY"])
        else:
            attrs["X"] = fmt(kf["anchorX"])
            attrs["Y"] = fmt(kf["anchorY"])
    elif tag == "IntFrame":
        attrs["Value"] = str(int(kf["alpha"]))
    elif tag == "BoolFrame":
        attrs["Value"] = "True" if kf["visible"] else "False"
    elif tag == "ColorFrame":
        r, g, b = kf["color"]
        a = int(kf.get("colorAlpha", 255))
        attrs["Alpha"] = str(a)
        kids.append(("Color", {"A": str(a), "R": str(int(r)),
                               "G": str(int(g)), "B": str(int(b))}, None))
    elif tag == "TextureFrame":
        plist = kf.get("plist") or ""
        ftype = "MarkedSubImage" if plist else "Normal"
        kids.append(("TextureFile", {"Type": ftype, "Path": kf["image"], "Plist": plist}, None))
    elif tag == "BlendFuncFrame":
        attrs["Src"] = str(int(kf["src"]))
        attrs["Dst"] = str(int(kf["dst"]))
    elif tag == "EventFrame":
        attrs["Value"] = kf.get("event") or ""
    elif tag == "InnerActionFrame":
        attrs["InnerActionType"] = kf.get("innerType") or "SingleFrame"
        # Cocos 自己的拼字就是 Aniamtion，必須照寫，否則編輯器認不得
        attrs["CurrentAniamtionName"] = kf.get("animation") or "-- ALL --"
        attrs["SingleFrameIndex"] = str(int(kf.get("innerFrame") or 0))

    if not tween:
        # Tween 放在 FrameIndex 後面、座標／Value 前面，跟編輯器存檔一致
        ordered = {"FrameIndex": attrs.pop("FrameIndex"), "Tween": "False"}
        ordered.update(attrs)
        attrs = ordered

    if tween:
        easing = int(kf.get("easing") or 0)
        points = kf.get("easingPoints")
        ease_kids = None
        if points:
            easing = -1
            ease_kids = [("Points", {}, [
                ("PointF", {"X": fmt(p[0]), "Y": fmt(p[1])}, None) for p in points
            ])]
        # EasingData 在 Color 等子元素前面
        kids = [("EasingData", {"Type": str(easing)}, ease_kids)] + kids

    return (tag, attrs, kids or None)


def _xml_lines(tag: str, attrs: dict, children=None) -> list:
    head = b"<" + tag.encode()
    for key, value in attrs.items():
        head += b" " + key.encode() + b'="' + escape_attr(value) + b'"'
    if not children:
        return [head + b" />"]
    lines = [head + b">"]
    for child in children:
        ctag, cattrs, ckids = child
        for line in _xml_lines(ctag, cattrs, ckids):
            lines.append(b"  " + line)
    lines.append(b"</" + tag.encode() + b">")
    return lines


def _read_frame(el, kind: str) -> dict:
    item = {
        "frame": int(num(el.get("FrameIndex"))),
        "tween": True if el.get("Tween") is None else _as_bool(el.get("Tween")),
    }
    easing = el.child("EasingData")
    if easing is not None:
        item["easing"] = int(num(easing.get("Type")))
        points_el = easing.child("Points")
        if points_el is not None:
            pts = [(num(p.get("X")), num(p.get("Y"))) for p in points_el.children]
            if pts:
                item["easingPoints"] = pts
    if kind == "position":
        item["x"] = num(el.get("X"))
        item["y"] = num(el.get("Y"))
    elif kind == "scale":
        item["scaleX"] = num(el.get("X"), 1.0)
        item["scaleY"] = num(el.get("Y"), 1.0)
    elif kind == "rotation":
        item["rotationX"] = num(el.get("X"))
        item["rotationY"] = num(el.get("Y"))
    elif kind == "anchor":
        item["anchorX"] = num(el.get("X"))
        item["anchorY"] = num(el.get("Y"))
    elif kind == "alpha":
        item["alpha"] = int(num(el.get("Value")))
    elif kind == "color":
        color = el.child("Color") or el.child("RenderColor")
        item["color"] = [
            int(num(color.get("R") if color else None, 255)),
            int(num(color.get("G") if color else None, 255)),
            int(num(color.get("B") if color else None, 255)),
        ]
        item["alpha"] = int(num(el.get("Alpha"), 255))
    elif kind == "visible":
        item["visible"] = _as_bool(el.get("Value"))
    elif kind == "image":
        tex = el.child("TextureFile") or el.child("FileData")
        item["image"] = (tex.get("Path") if tex else "") or ""
        item["plist"] = (tex.get("Plist") if tex else "") or ""
    elif kind == "blend":
        item["src"] = int(num(el.get("Src"), 770))
        item["dst"] = int(num(el.get("Dst"), 771))
    elif kind == "event":
        item["event"] = el.get("Value") or ""
    elif kind == "inner":
        item["innerType"] = el.get("InnerActionType") or "SingleFrame"
        item["animation"] = (el.get("CurrentAniamtionName")
                             or el.get("CurrentAnimationName") or "")
        item["innerFrame"] = int(num(el.get("SingleFrameIndex")))
    return item
