"""寫動畫時間軸的測試。

全程在暫存目錄做，不碰真專案。驗的是「寫進去的 XML 跟編輯器存的結構一致，
而且 csdview 求值出來的座標／貼圖是對的」——光看 Duration 數字不夠。

用法：python tests/test_anim.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def check(name: str, condition, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"[{'  ok  ' if condition else ' FAIL '}] {name}"
          + (f"  — {detail}" if detail and not condition else ""))


def main() -> int:
    stage = tempfile.mkdtemp(prefix="csdmcp_anim_")
    try:
        path = os.path.join(stage, "Icon.csd")
        run_checks(path)
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    print(f"\n通過 {len(PASS)}，失敗 {len(FAIL)}")
    if FAIL:
        print("失敗項目：" + "、".join(FAIL))
    return 1 if FAIL else 0


def run_checks(path: str) -> None:
    from csdmcp.editor import Csd, create
    from csdmcp import anim as anim_mod
    from csdview.parser import parse
    from csdview.anim import AnimationState

    create(path, "node")
    doc = Csd(path)
    doc.add_node(doc.resolve(None), "sprite", "icon",
                 {"x": 0, "y": -15, "scaleX": 1.05, "scaleY": 1.05,
                  "image": "mod/img/icon.png"})
    doc.add_node(doc.resolve(None), "sprite", "title",
                 {"x": 0, "y": 0, "image": "mod/img/title.png"})
    doc.save("底")

    doc = Csd(path)
    icon = doc.resolve("icon")
    anim_mod.set_track(doc, icon, "position", [
        {"frame": 0, "x": 0, "y": -15, "easing": 5},
        {"frame": 9, "y": -6, "easing": 2},
        {"frame": 18, "y": -15},
        {"frame": 65, "y": -15},
    ])
    anim_mod.set_track(doc, icon, "scale", [
        {"frame": 0, "scaleX": 1.05, "scaleY": 1.05},
        {"frame": 9, "scaleY": 1.15},
        {"frame": 18, "scaleY": 0.95},
        {"frame": 22, "scaleY": 1.05},
    ])
    anim_mod.set_track(doc, icon, "rotation", [
        {"frame": 0, "rotation": 0},
        {"frame": 18, "rotation": -5},
        {"frame": 29, "rotation": 5},
        {"frame": 35, "rotation": 0},
    ])
    light = doc.resolve("title")
    anim_mod.set_track(doc, light, "image", [
        {"frame": 51, "image": "mod/img/a.png", "plist": "mod/img/pack.plist"},
        {"frame": 54, "image": "mod/img/b.png", "plist": ""},
        {"frame": 75, "image": "mod/img/c.png", "plist": "mod/img/pack.plist"},
    ])
    anim_mod.set_track(doc, light, "alpha", [
        {"frame": 51, "alpha": 178},
        {"frame": 63, "alpha": 229},
        {"frame": 75, "alpha": 0},
    ])
    anim_mod.set_clips(doc, [{"name": "idle", "start": 0, "end": 75}])
    anim_mod.set_duration(doc, 75)
    saved = doc.save("動畫")
    check("有寫檔", saved.get("written") is True)

    raw = open(path, "rb").read()
    check("CRLF", b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""))
    check("Position 軌道用 PointFrame", b'Property="Position"' in raw
          and b"<PointFrame " in raw)
    check("easing 5 寫進去了", b'<EasingData Type="5" />' in raw)
    check("省略的 x 從上一格接著", b'X="0.0000" Y="-6.0000"' in raw)
    check("貼圖序列 Tween=False", b'<TextureFrame FrameIndex="51" Tween="False">' in raw)
    check("有 plist 的寫成 MarkedSubImage",
          b'Type="MarkedSubImage" Path="mod/img/a.png" Plist="mod/img/pack.plist"' in raw)
    check("沒 plist 的寫成 Normal",
          b'Type="Normal" Path="mod/img/b.png" Plist=""' in raw)
    check("Duration 是 75", b'<Animation Duration="75"' in raw)
    check("有 AnimationList", b'<AnimationInfo Name="idle" StartIndex="0" EndIndex="75">' in raw)
    check("Cocos 的 Aniamtion 拼字", b"CurrentAniamtionName" not in raw)  # 這份沒寫 inner

    parsed = parse(path)
    check("csdview 讀得動", parsed.duration == 75, str(parsed.duration))
    check("csdview 看到片段", [c.name for c in parsed.clips] == ["idle"],
          str([c.name for c in parsed.clips]))

    icon_tag = int(Csd(path).elem_of(Csd(path).resolve("icon").node_path).get("ActionTag"))
    title_tag = int(Csd(path).elem_of(Csd(path).resolve("title").node_path).get("ActionTag"))

    at0 = AnimationState(parsed, 0).track(icon_tag)
    at9 = AnimationState(parsed, 9).track(icon_tag)
    at18 = AnimationState(parsed, 18).track(icon_tag)
    check("第 0 格 Y 是 -15", at0 and abs(at0.position[1] - (-15)) < 1e-4, str(at0.position if at0 else None))
    check("第 9 格 Y 是 -6", at9 and abs(at9.position[1] - (-6)) < 1e-4, str(at9.position if at9 else None))
    check("第 18 格 Y 回到 -15", at18 and abs(at18.position[1] - (-15)) < 1e-4,
          str(at18.position if at18 else None))
    check("第 9 格 scaleY 是 1.15", at9 and abs(at9.scale[1] - 1.15) < 1e-4,
          str(at9.scale if at9 else None))
    check("第 18 格旋轉 -5", at18 and abs(at18.rotation[0] - (-5)) < 1e-4,
          str(at18.rotation if at18 else None))

    tex = AnimationState(parsed, 54).track(title_tag)
    check("第 54 格換成 b.png", tex and tex.file and tex.file.path.endswith("b.png"),
          str(getattr(tex.file, "path", None) if tex else None))
    a75 = AnimationState(parsed, 75).track(title_tag)
    check("第 75 格 alpha 是 0", a75 and a75.alpha == 0, str(a75.alpha if a75 else None))

    listing = anim_mod.list_tracks(Csd(path))
    props = {(t["node"].split("/")[-1], t["prop"]) for t in listing["tracks"]}
    check("list_anim 看得到寫進去的軌道",
          props >= {("icon", "position"), ("icon", "scale"), ("icon", "rotation"),
                    ("title", "image"), ("title", "alpha")},
          str(props))
    check("list_anim 的 duration", listing["duration"] == 75, str(listing["duration"]))

    # 覆寫一條軌道、空 frames 刪掉
    doc = Csd(path)
    anim_mod.set_track(doc, doc.resolve("icon"), "position", [
        {"frame": 0, "x": 10, "y": 20},
        {"frame": 10, "x": 30, "y": 40},
    ])
    doc.save("覆寫 position")
    again = anim_mod.list_tracks(Csd(path), Csd(path).resolve("icon"))
    pos = next(t for t in again["tracks"] if t["prop"] == "position")
    check("覆寫後只剩新的兩格", len(pos["frames"]) == 2, str(pos["frames"]))
    check("覆寫後第一格是新值", pos["frames"][0]["x"] == 10 and pos["frames"][0]["y"] == 20)

    doc = Csd(path)
    anim_mod.set_track(doc, doc.resolve("icon"), "rotation", [])
    doc.save("刪 rotation")
    left = {t["prop"] for t in anim_mod.list_tracks(Csd(path), Csd(path).resolve("icon"))["tracks"]}
    check("空 frames 會刪掉那條軌道", "rotation" not in left, str(left))

    # duration 不能比最晚影格短
    doc = Csd(path)
    try:
        anim_mod.set_duration(doc, 1)
        check("duration 太短會被擋", False)
    except Exception as exc:
        check("duration 太短會被擋", "最晚的關鍵影格" in str(exc), str(exc))

    # 重複 frame 要報錯，不能默默留下一格
    doc = Csd(path)
    try:
        anim_mod.set_track(doc, doc.resolve("icon"), "alpha", [
            {"frame": 0, "alpha": 255},
            {"frame": 0, "alpha": 0},
        ])
        check("重複 frame 會被擋", False)
    except Exception as exc:
        check("重複 frame 會被擋", "出現兩次" in str(exc), str(exc))

    # MCP 工具層
    from csdmcp import tools
    payload, _ = tools.call("list_anim", {"csd": path})
    check("tools.list_anim 通", payload.get("count", 0) >= 1, str(payload.get("count")))
    payload, _ = tools.call("set_anim", {
        "csd": path, "node": "icon", "prop": "alpha",
        "frames": [{"frame": 0, "alpha": 255}, {"frame": 12, "alpha": 128}],
    })
    check("tools.set_anim 通", payload.get("written") is True)
    listed = tools.call("list_anim", {"csd": path, "node": "icon"})[0]
    alpha = next(t for t in listed["tracks"] if t["prop"] == "alpha")
    check("工具寫進去的 alpha 看得到", alpha["frames"][-1]["alpha"] == 128)


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        import traceback
        traceback.print_exc()
        code = 1
    sys.exit(code)
