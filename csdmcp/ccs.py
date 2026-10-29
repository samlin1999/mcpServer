"""把 .csd 登錄進 Cocos Studio 的專案檔（.ccs）。

新建一個 .csd 檔案只成功了一半：`.ccs` 裡有一份完整的資源樹，
每個檔案都寫成 `<Project Name="X.csd" Type="Layer" />`。**沒登錄進去，
用 Cocos Studio 開專案時左邊的資源列表就看不到這個檔案**，等於白做。

編輯方式跟 .csd 一樣走位元組層級的置換：.ccs 動輒上萬行、而且在 git 裡，
整份重寫會產生沒人審得動的 diff。
"""
from __future__ import annotations

import glob
import os
import re
import tempfile

from .locking import ReplaceBusy, file_lock, replace_with_retry
from .xmldoc import Xml, escape_attr

# 資源樹裡同一層的排法：資料夾在前、檔案在後，各自照名稱排序（跟編輯器存出來一致）
_FOLDER = "Folder"
_PROJECT = "Project"


class CcsError(Exception):
    pass


def find_solution(csd_root: str) -> "str | None":
    """找出這個 cocosstudio 根目錄對應的 .ccs。

    慣例是 .ccs 放在 cocosstudio 的上一層（<專案>/Inanna.ccs 對
    <專案>/Inanna/cocosstudio/）。找不到就往上再找一層。
    """
    here = os.path.abspath(csd_root)
    for _ in range(3):
        parent = os.path.dirname(here)
        found = sorted(glob.glob(os.path.join(parent, "*.ccs")))
        if found:
            return found[0]
        if parent == here:
            break
        here = parent
    return None


def project_version(solution: "str | None") -> str:
    """讀 .ccs 裡的版本號，讓新建的 .csd 跟專案其他檔案一致。"""
    if solution and os.path.isfile(solution):
        try:
            with open(solution, "rb") as fh:
                head = fh.read(600).decode("utf-8", "replace")
            found = re.search(r'<PropertyGroup[^>]*Version="([^"]+)"', head)
            if found:
                return found.group(1)
        except OSError:
            pass
    return "3.10.0.0"


def _root_folder(xml: Xml):
    found = xml.find_first("RootFolder")
    if found is not None:
        return found
    raise CcsError("這個 .ccs 裡找不到 <RootFolder>，格式跟預期不同。")


def _find_child(el, tag: str, name: str):
    for child in el.children_named(tag):
        if (child.get("Name") or "").lower() == name.lower():
            return child
    return None


def _insert_slot(folder, tag: str, name: str):
    """算出新項目要插在哪裡，回傳 (參考的兄弟節點, 是否插在它之前)。

    同類的照名稱排序插入，而且資料夾一律排在檔案之前——照著編輯器的排法，
    下次它存檔時才不會把整棵樹重排成一大包 diff。
    兩個方向要分開講：插在某個項目「之前」是「內容 + 換行 + 縮排」，
    插在「之後」是「換行 + 縮排 + 內容」，寫反會把兩個項目擠到同一行。
    """
    same = folder.children_named(tag)
    for child in same:
        if (child.get("Name") or "").lower() > name.lower():
            return child, True
    if same:
        return same[-1], False
    if tag == _PROJECT:
        folders = folder.children_named(_FOLDER)
        if folders:
            return folders[-1], False
    return (folder.children[0], True) if folder.children else (None, False)


def _insert(xml: Xml, folder_route: tuple, tag: str, attrs: dict) -> None:
    folder = xml.at(folder_route)
    line = b"<" + tag.encode()
    for key, value in attrs.items():
        line += b" " + key.encode() + b'="' + escape_attr(value) + b'"'
    line += b" />"

    if folder.self_closing:
        indent = xml.indent_of(folder) + b"  "
        body = (b">" + xml.newline + indent + line + xml.newline
                + xml.indent_of(folder) + b"</" + folder.tag.encode() + b">")
        xml.splice([(xml.open_tag_cut(folder), folder.tag_end, body)])
        return

    sibling, before = _insert_slot(folder, tag, attrs["Name"])
    if sibling is None:
        indent = xml.indent_of(folder) + b"  "
        point = folder.tag_end
        xml.splice([(point, point, xml.newline + indent + line)])
        return

    indent = xml.indent_of(sibling)
    if before:
        xml.splice([(sibling.start, sibling.start, line + xml.newline + indent)])
    else:
        xml.splice([(sibling.end, sibling.end, xml.newline + indent + line)])


def register(solution: str, csd_root: str, csd_path: str, root_type: str) -> dict:
    """把一個 .csd 寫進 .ccs 的資源樹，沿路缺的資料夾一併補上。

    已經登錄過就什麼都不做，重跑不會產生重複項目。
    """
    if not os.path.isfile(solution):
        raise CcsError(f"找不到專案檔：{solution}")
    relative = os.path.relpath(os.path.abspath(csd_path),
                              os.path.abspath(csd_root)).replace("\\", "/")
    if relative.startswith(".."):
        raise CcsError(f"{csd_path} 不在 {csd_root} 底下，沒辦法登錄。")
    parts = relative.split("/")
    folders, filename = parts[:-1], parts[-1]

    # 讀到寫之間有一段空窗，同時有兩筆登錄的話後寫的會蓋掉前一筆。
    # 鎖擋得住另一個 csdmcp 行程；擋不住 Cocos Studio，所以另外比對修改時間。
    with file_lock(solution):
        before = _stamp(solution)
        with open(solution, "rb") as fh:
            xml = Xml(fh.read())

        route = _root_folder(xml).route
        created: list = []
        for name in folders:
            existing = _find_child(xml.at(route), _FOLDER, name)
            if existing is None:
                _insert(xml, route, _FOLDER, {"Name": name})
                created.append(name)
                existing = _find_child(xml.at(route), _FOLDER, name)
            route = existing.route

        if _find_child(xml.at(route), _PROJECT, filename) is not None:
            return {"solution": solution, "entry": relative, "added": False,
                    "note": "這個檔案已經登錄過了。"}

        _insert(xml, route, _PROJECT, {"Name": filename, "Type": root_type})

        # 寫回去之前先確認還是合法的 XML；.ccs 壞掉會讓整個專案打不開
        Xml(xml.data)
        _guard(solution, before)
        _write_atomic(solution, xml.data)
    return {"solution": solution, "entry": relative, "added": True,
            "foldersCreated": created, "type": root_type}


def unregister(solution: str, csd_root: str, csd_path: str) -> dict:
    """把一個 .csd 從 .ccs 的資源樹裡拿掉（空掉的資料夾保留，編輯器容許）。"""
    if not os.path.isfile(solution):
        raise CcsError(f"找不到專案檔：{solution}")
    relative = os.path.relpath(os.path.abspath(csd_path),
                              os.path.abspath(csd_root)).replace("\\", "/")
    parts = relative.split("/")

    with file_lock(solution):
        before = _stamp(solution)
        with open(solution, "rb") as fh:
            xml = Xml(fh.read())
        route = _root_folder(xml).route
        for name in parts[:-1]:
            found = _find_child(xml.at(route), _FOLDER, name)
            if found is None:
                return {"solution": solution, "entry": relative, "removed": False}
            route = found.route
        entry = _find_child(xml.at(route), _PROJECT, parts[-1])
        if entry is None:
            return {"solution": solution, "entry": relative, "removed": False}

        xml.remove(entry.route)
        Xml(xml.data)
        _guard(solution, before)
        _write_atomic(solution, xml.data)
    return {"solution": solution, "entry": relative, "removed": True}


def _stamp(path: str):
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def _guard(path: str, before) -> None:
    """換檔之前確認 .ccs 沒有被別人改過（多半是 Cocos Studio 自己存了一次）。"""
    if _stamp(path) != before:
        raise CcsError(
            f"{os.path.basename(path)} 在這期間被別的程式改過"
            "（可能是 Cocos Studio 存了檔）。為了不蓋掉對方的改動，這次登錄已經放棄。"
            "請關掉編輯器或重試一次。")


def _write_atomic(path: str, data: bytes) -> None:
    handle, temp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".ccs.tmp")
    try:
        with os.fdopen(handle, "wb") as fh:
            fh.write(data)
        replace_with_retry(temp, path)
    except ReplaceBusy as exc:
        if os.path.exists(temp):
            os.remove(temp)
        raise CcsError(f"{exc} 這次登錄沒有寫入，.ccs 沒有動——"
                       "檔案本身可能已經建好了，關掉編輯器後用 register 補登錄。") from exc
    except BaseException:
        if os.path.exists(temp):
            os.remove(temp)
        raise
