"""位元組層級的 XML 索引與外科手術式編輯。

`.csd` 是 Cocos Studio 產生的 XML。用 ElementTree 讀進來再整份寫回會重排格式：
縮排、屬性順序、CRLF、空元素的寫法全部會變，於是 git 上出現一份沒人審得動的 diff，
也可能悄悄動到我們根本沒打算改的欄位。

所以這一層只做一件事：記下每個元素與屬性在**原始位元組**裡的位置，
改動時只置換那一小段位元組，其餘一個 byte 都不碰。

為什麼是位元組而不是字元：expat 回報的位移（CurrentByteIndex）是位元組位移，
而 .csd 裡有中文文案，字元位移與位元組位移對不起來。全程用 bytes 就沒有這個換算問題。
"""
from __future__ import annotations

import re
import xml.parsers.expat as expat
from dataclasses import dataclass, field

_GT = 0x3E      # >
_QUOTE = 0x22   # "
_APOS = 0x27    # '


@dataclass
class Elem:
    """一個元素在原始位元組裡的座標。

    route 是「從根一路下來的第幾個子元素」構成的索引串，例如 (0, 1, 0)。
    每次改動都會讓所有位移失效、必須重建索引；但只要結構沒變，route 就還指向同一個元素——
    所以對外一律用 route 當作元素的身分，不要跨改動保留 Elem 物件。
    """
    tag: str
    attrib: dict
    start: int          # '<' 的位移
    tag_end: int        # 起始標籤結尾 '>' 的下一個位元組
    end: int            # 整個元素結束後的下一個位元組
    self_closing: bool
    route: tuple = ()
    parent: "Elem | None" = None
    children: list = field(default_factory=list)

    def child(self, tag: str) -> "Elem | None":
        for c in self.children:
            if c.tag == tag:
                return c
        return None

    def children_named(self, tag: str) -> list:
        return [c for c in self.children if c.tag == tag]

    def get(self, key: str, default=None):
        return self.attrib.get(key, default)


def _scan_tag_end(data: bytes, i: int) -> int:
    """從 '<' 掃到這個標籤的 '>'，回傳 '>' 的下一個位置。

    要跳過引號內的內容：屬性值裡出現 '>' 是合法的（例如文案寫了 "a > b"）。
    """
    quote = 0
    j = i
    n = len(data)
    while j < n:
        c = data[j]
        if quote:
            if c == quote:
                quote = 0
        elif c in (_QUOTE, _APOS):
            quote = c
        elif c == _GT:
            return j + 1
        j += 1
    raise XmlParseError(f"位移 {i} 的標籤沒有結尾（檔案可能被截斷）。"
                        "這次沒有做任何改動。")


def build_index(data: bytes) -> Elem:
    """用 expat 掃一遍，建出帶位元組位移的元素樹。"""

    # 這一層的整套位移運算都假設 UTF-8：搜尋結束標籤用的是 ASCII 位元組
    # （data.find(b"</tag")），插入的內容也是 escape_attr 產出的 UTF-8。
    # UTF-16 內容 expat 自己讀得懂（它認 BOM），於是會「解析成功」而在
    # 找結束標籤時才失敗，錯誤訊息看起來像檔案被截斷——完全指錯方向。
    # 所以在最前面就擋掉：.csd 一律是 UTF-8，這不是需要支援的變體。
    if data[:4] in (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"):
        raise XmlParseError("這個檔是 UTF-32；.csd 必須是 UTF-8。"
                            "請用 Cocos Studio 或編輯器另存成 UTF-8。"
                            "這次沒有做任何改動。")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00<" in data[:64]:
        raise XmlParseError("這個檔是 UTF-16；.csd 必須是 UTF-8。"
                            "請用 Cocos Studio 或編輯器另存成 UTF-8。"
                            "這次沒有做任何改動。")

    parser = expat.ParserCreate()
    stack: list[Elem] = []
    root: list = [None]

    def on_start(name, attrs):
        start = parser.CurrentByteIndex
        tag_end = _scan_tag_end(data, start)
        parent = stack[-1] if stack else None
        # 自閉合與否直接看位元組。expat 結束事件的 CurrentByteIndex 對空元素
        # 不是指回同一個標籤，拿它判斷會把 <Size ... /> 的範圍延伸到下一個元素上，
        # 之後的置換就會吃掉不該碰的內容。
        el = Elem(tag=name, attrib=dict(attrs), start=start, tag_end=tag_end,
                  end=tag_end, self_closing=data[tag_end - 2:tag_end] == b"/>",
                  parent=parent)
        if parent is None:
            root[0] = el
            el.route = ()
        else:
            el.route = parent.route + (len(parent.children),)
            parent.children.append(el)
        stack.append(el)

    def on_end(_name):
        el = stack.pop()
        if el.self_closing:
            el.end = el.tag_end
            return
        # 從最後一個子元素之後往前找結束標籤。子元素一定先於父元素關閉，
        # 所以此時它們的 end 都算好了，同名巢狀（Children 裡還有 Children）也不會找錯。
        after = el.children[-1].end if el.children else el.tag_end
        close = data.find(b"</" + el.tag.encode(), after)
        if close < 0:
            raise XmlParseError(f"找不到 <{el.tag}> 的結束標籤（檔案可能被截斷）。"
                                "這次沒有做任何改動。")
        el.end = _scan_tag_end(data, close)

    parser.StartElementHandler = on_start
    parser.EndElementHandler = on_end
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        # expat 的原始訊息只有位置，看不出原因；_parse_hint 補上「為什麼」。
        # 不翻譯的話這條路會一路冒到 server 的 catch-all，模型只看到
        # 「未預期的錯誤：ExpatError: not well-formed (invalid token): line 3, column 3」。
        raise XmlParseError(_parse_hint(data, str(exc))) from exc
    if root[0] is None:
        raise XmlParseError("這個檔沒有 XML 根元素（可能是空檔）。這次沒有做任何改動。")
    return root[0]


class Xml:
    """一份可外科手術式編輯的 XML 位元組內容。"""

    def __init__(self, data: bytes):
        self.data = data
        self.root = build_index(data)
        # 檔案用什麼換行就跟著用什麼：.csd 一律是 CRLF，改成 LF 會讓整份檔案變成 diff
        self.newline = b"\r\n" if b"\r\n" in data[:4096] else b"\n"
        # {tag: [屬性慣用順序]}，新屬性照這個順序插入而不是一律接在最後。
        # 由上層填入（見 layout.ATTR_ORDER），這一層不該知道 Cocos 的格式慣例。
        self.attr_order: dict = {}

    # ------------------------------------------------------------ 定位

    def at(self, route: tuple) -> Elem:
        el = self.root
        for i in route:
            el = el.children[i]
        return el

    def find_first(self, tag: str, el: "Elem | None" = None) -> "Elem | None":
        """找出最外層的某個 tag。找不到回 None。

        刻意用廣度優先，不是 `next(e for e in walk() if ...)`：.csd 把
        `<Animation>`（整條時間軸，動輒幾千個元素）排在 `<ObjectData>`
        **前面**，所以深度優先要先走完整個 Animation 子樹才碰到 ObjectData。
        實測 540 KB 的檔上，一次 list_anim 呼叫裡這個查找被做了 221 次、
        合計走訪 112 萬次，佔掉那個呼叫的全部時間。

        廣度優先還有一個語意上的好處：回傳的是**最淺**的那個。
        ObjectData 會巢狀（子節點也是 ObjectData），我們要的一直是最外層那個。
        """
        queue = [self.root if el is None else el]
        head = 0
        while head < len(queue):
            current = queue[head]
            head += 1
            if current.tag == tag:
                return current
            queue.extend(current.children)
        return None

    def walk(self, el: "Elem | None" = None):
        """前序走訪所有元素。

        刻意用明確的堆疊而不是遞迴的 `yield from`：那樣每一層巢狀都要佔一個
        Python frame，巢狀夠深的檔案（損壞或惡意的內容真的做得到）會丟
        RecursionError——那是解譯器層級的錯誤，翻不成有用的訊息。
        順帶也快一些：`yield from` 串起來的話，每吐出一個元素都要穿過整條
        委派鏈，深樹會退化成 O(節點數 x 深度)。
        """
        stack = [self.root if el is None else el]
        while stack:
            current = stack.pop()
            yield current
            # 反著推進去，吐出來的順序才跟原本的遞迴一樣（前序、子節點依序）
            stack.extend(reversed(current.children))

    # ------------------------------------------------------------ 基本改動

    def splice(self, cuts: list) -> None:
        """一次套用多段置換，然後重建一次索引。

        **一筆編輯要把所有 cuts 收齊後只呼叫這裡一次。** 重建索引是整份檔案重新解析，
        540 KB 的 .csd 要 55 ms；每改一個欄位就呼叫一次的話，光是改個尺寸連帶重算
        28 個子節點就會做將近 90 次重建。

        插入點相同的零寬 cut 會先合併成一筆，順序照傳進來的順序。
        不合併的話它們會在「由後往前套用」時被倒過來，
        `<Position />` 同時補 X 與 Y 就會寫成 `Y="…" X="…"`。
        """
        if not cuts:
            return
        for start, end, _new in cuts:
            if start < 0 or end > len(self.data) or start > end:
                raise ValueError(f"置換範圍不合法: {start}~{end}")

        merged: list = []
        inserts: dict = {}
        for cut in cuts:
            start, end, new = cut
            if start == end:
                if start in inserts:
                    merged[inserts[start]] = (start, start, merged[inserts[start]][2] + new)
                    continue
                inserts[start] = len(merged)
            merged.append(cut)

        ordered = sorted(merged, key=lambda c: c[0], reverse=True)
        for a, b in zip(ordered, ordered[1:]):
            if b[1] > a[0]:
                raise ValueError("置換範圍互相重疊")
        data = self.data
        for start, end, new in ordered:
            data = data[:start] + new + data[end:]
        self.data = data
        self.root = build_index(data)

    def _attr_cuts(self, el: Elem, values: dict) -> list:
        """把「設定／刪除起始標籤上的屬性」換算成位元組置換。

        值是 None 代表刪掉這個屬性；屬性本來就不存在時，插在標籤結尾之前。
        """
        span = self.data[el.start:el.tag_end]
        base = el.start
        cuts: list = []
        order = self.attr_order.get(el.tag) or []
        rank = {name: i for i, name in enumerate(order)}

        # 先量出現有屬性各自佔了哪一段，新屬性才知道該插在誰後面
        spans: dict = {}
        for found in re.finditer(rb'\s([A-Za-z_][\w.:-]*)\s*=\s*"([^"]*)"', span):
            spans[found.group(1).decode("ascii")] = (found.start(), found.end(),
                                                     found.start(2), found.end(2))

        additions: list = []
        for name, value in values.items():
            here = spans.get(name)
            if here:
                if value is None:
                    cuts.append((base + here[0], base + here[1], b""))
                else:
                    cuts.append((base + here[2], base + here[3], escape_attr(value)))
            elif value is not None:
                additions.append(name)

        # 新屬性先照慣用順序排好，再依插入點分組合併成單一 cut。
        # 分組是必要的：同一個插入點放多筆 cut，套用時會被倒過來變成反序。
        additions.sort(key=lambda n: rank.get(n, len(order)))
        grouped: dict = {}
        for name in additions:
            point = self._insert_point(el, span, spans, rank, name)
            text = b" " + name.encode() + b'="' + escape_attr(values[name]) + b'"'
            grouped[point] = grouped.get(point, b"") + text
        for point, text in grouped.items():
            cuts.append((base + point, base + point, text))
        return cuts

    def attr_cuts(self, route: tuple, values: dict) -> list:
        """算出設定某個元素屬性所需的 cuts，但不套用——給批次編輯用。"""
        return self._attr_cuts(self.at(route), values) if values else []

    def element_cut(self, parent_route: tuple, tag: str, attrs: dict,
                    order: list) -> list:
        """算出「在某個父元素底下新增一個空元素」所需的 cuts，但不套用。

        父元素是自閉合時回傳 None，代表得先展開成一對標籤——那個動作會改到
        起始標籤本身，跟屬性 cuts 有重疊風險，所以交給呼叫端單獨處理。
        """
        parent = self.at(parent_route)
        if parent.self_closing:
            return None

        line = b"<" + tag.encode()
        for key, value in attrs.items():
            line += b" " + key.encode() + b'="' + escape_attr(value) + b'"'
        line += b" />"

        rank = {name: i for i, name in enumerate(order)}
        mine = rank.get(tag, len(order))
        after = None
        for child in parent.children:
            if rank.get(child.tag, len(order)) <= mine:
                after = child
            else:
                break
        if after is not None:
            indent = self.indent_of(after)
            return [(after.end, after.end, self.newline + indent + line)]
        if parent.children:
            indent = self.indent_of(parent.children[0])
            point = parent.children[0].start
            return [(point, point, line + self.newline + indent)]
        indent = self.indent_of(parent) + b"  "
        return [(parent.tag_end, parent.tag_end, self.newline + indent + line)]

    def remove_cut(self, route: tuple) -> tuple:
        """算出刪除一個元素（連同它獨占那一行）所需的 cut，但不套用。"""
        el = self.at(route)
        start = el.start
        line_start = self.data.rfind(b"\n", 0, start) + 1
        if self.data[line_start:start].strip() == b"":
            start = line_start
            if start >= 2 and self.data[start - 2:start] == b"\r\n":
                start -= 2
            elif start >= 1 and self.data[start - 1:start] == b"\n":
                start -= 1
        return (start, el.end, b"")

    def _insert_point(self, el: Elem, span: bytes, spans: dict,
                      rank: dict, name: str) -> int:
        """新屬性該插在起始標籤裡的哪個位置（回傳相對於標籤起點的位移）。

        照慣用順序插到第一個「排在它後面」的現有屬性之前；順序表沒提到就接在最後，
        但仍會排在 ctype 前面——Cocos Studio 一律把 ctype 寫在最末。
        """
        mine = rank.get(name)
        if mine is not None:
            later = [spans[other][0] for other, index in
                     ((o, rank.get(o)) for o in spans)
                     if index is not None and index > mine]
            if later:
                return min(later)
        if "ctype" in spans and name != "ctype":
            return spans["ctype"][0]

        # 接在標籤最後：跳過結尾的 '/' 與空白，維持原本的排版
        insert = el.tag_end - 1 - el.start
        if el.self_closing and span[insert - 1:insert] == b"/":
            insert -= 1
        while insert > 0 and span[insert - 1:insert] in (b" ", b"\t"):
            insert -= 1
        return insert

    def set_attrs(self, route: tuple, values: dict) -> None:
        """一次設定同一個元素上的多個屬性（值為 None 表示刪除）。"""
        if not values:
            return
        self.splice(self._attr_cuts(self.at(route), values))

    # ------------------------------------------------------------ 子元素

    def indent_of(self, el: Elem) -> bytes:
        """取這個元素所在行的縮排。"""
        line_start = self.data.rfind(b"\n", 0, el.start) + 1
        return self.data[line_start:el.start]

    def open_tag_cut(self, el: Elem) -> int:
        """要把 `<A ... />` 展開成 `<A ...>…</A>` 時，置換該從哪個位元組開始。

        回傳的是結尾 ` />` 的起點：從 '/' 往前把空白也吃掉，
        展開後才是乾淨的 `<A ...>` 而不是 `<A ... >`。
        直接從 `tag_end - 1`（也就是 '>'）開始置換是會出事的寫法——
        '/' 會被留下來，變成 `<A /></A>`，整份檔案就不是合法 XML 了。
        """
        cut = el.tag_end - 2                      # tag_end - 1 是 '>'，前一個是 '/'
        while cut > el.start and self.data[cut - 1:cut] in (b" ", b"\t"):
            cut -= 1
        return cut

    def ensure_child(self, route: tuple, tag: str, order: list) -> tuple:
        """確保某個子元素存在，回傳它的 route。

        order 是這一層子元素的慣用順序（Cocos Studio 存檔的順序）。
        新元素插到順序表裡它該在的位置，讓檔案看起來跟編輯器存出來的一樣。
        功能上 cocos 與 csdview 都是用 tag 名稱查找，順序不影響行為。
        """
        el = self.at(route)
        existing = el.child(tag)
        if existing is not None:
            return existing.route

        indent = self.indent_of(el)
        inner = indent + b"  "
        fragment = b"<" + tag.encode() + b" />"

        if el.self_closing:
            # <A ... /> 要先展開成 <A ...>\n  <tag />\n</A>
            body = (b">" + self.newline + inner + fragment + self.newline
                    + indent + b"</" + el.tag.encode() + b">")
            self.splice([(self.open_tag_cut(el), el.tag_end, body)])
            return self.at(route).child(tag).route

        # 插入點：順序表裡排在 tag 之前的最後一個現有子元素之後
        rank = {name: i for i, name in enumerate(order)}
        mine = rank.get(tag, len(order))
        after = None
        for c in el.children:
            if rank.get(c.tag, len(order)) <= mine:
                after = c
            else:
                break
        point = after.end if after is not None else el.tag_end
        self.splice([(point, point, self.newline + inner + fragment)])
        return self.at(route).child(tag).route

    def insert_fragment(self, parent_route: tuple, lines: list,
                        before_route: "tuple | None" = None) -> None:
        """把一段已排好版的 XML 插進某個父元素（lines 不含縮排，會自動補）。"""
        parent = self.at(parent_route)
        indent = self.indent_of(parent) + b"  "
        body = self.newline.join(indent + line for line in lines)

        if before_route is not None:
            point = self.at(before_route).start
            self.splice([(point, point, body + self.newline + indent)])
            return

        if parent.self_closing:
            whole = (b">" + self.newline + body + self.newline
                     + self.indent_of(parent) + b"</" + parent.tag.encode() + b">")
            self.splice([(self.open_tag_cut(parent), parent.tag_end, whole)])
            return

        point = parent.children[-1].end if parent.children else parent.tag_end
        self.splice([(point, point, self.newline + body)])

    def remove(self, route: tuple) -> None:
        """刪掉一個元素，連同它獨占的那一行。"""
        self.splice([self.remove_cut(route)])

    def remove_many(self, routes: list) -> int:
        """一次刪掉多個元素，只重建一次索引。

        routes 必須互不包含（不能同時給父層與它的子層），否則範圍會重疊。
        """
        if not routes:
            return 0
        self.splice([self.remove_cut(r) for r in routes])
        return len(routes)

    def expand_self_closing(self, route: tuple) -> None:
        """把 `<A ... />` 展開成 `<A ...></A>`，讓它可以放子元素。"""
        el = self.at(route)
        if not el.self_closing:
            return
        body = b">" + self.newline + self.indent_of(el) + b"</" + el.tag.encode() + b">"
        self.splice([(self.open_tag_cut(el), el.tag_end, body)])

    def slice_of(self, route: tuple) -> bytes:
        el = self.at(route)
        return self.data[el.start:el.end]


# XML 1.0 允許的字元只有 Tab、換行、Enter，加上 #x20 以上這幾段。
# 其餘的控制字元、單獨的代理對、#xFFFE/#xFFFF 一律不合法——注意是**連寫成
# &#x1; 都不行**（那是 XML 1.1 才有的放寬），所以只能拒絕，不能轉義。
_ILLEGAL_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


class XmlError(ValueError):
    """這一層認得的錯誤：內容解析不了，或要寫進去的值不合法。

    共同父類的用途是讓 server 只要認一種類別。仍然繼承 ValueError，
    既有的 `except ValueError` 照樣接得到。
    """


class XmlParseError(XmlError):
    """這份內容不是合法的 XML，或結構超出這個工具處理得了的範圍。"""


class XmlValueError(XmlError):
    """要寫進 XML 的值本身不合法（例如夾了控制字元）。

    獨立一種類別，讓上層能把它當成「使用者做得到的修正」直接回給模型；
    以前這種輸入會一路寫成位元組，等到重新索引時才丟 ExpatError，
    模型只看到一句「not well-formed (invalid token): line 953, column 35」。
    """


def _parse_hint(data: bytes, detail: str) -> str:
    """把 expat 的訊息補成「看得出下一步做什麼」的說明。

    expat 只會說「not well-formed: line 3, column 3」。對模型（和人）來說
    真正需要的是「為什麼會這樣」——而現實中的原因就那幾種，全都認得出來。
    """
    causes = []
    if not data.strip():
        causes.append("這個檔是空的（0 個位元組或只有空白）")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00<" in data[:64]:
        causes.append("內容看起來是 UTF-16；.csd 必須是 UTF-8")
    if b"<<<<<<<" in data or b">>>>>>>" in data:
        causes.append("裡面有 git 衝突標記（<<<<<<< / >>>>>>>），這個檔還沒解完衝突")
    if b"\x00" in data:
        causes.append("裡面有 NUL 位元組，檔案可能損壞或根本不是文字檔")
    if "junk after document element" in detail:
        causes.append("根元素後面還有內容，多半是合併時把整份檔案接了兩次")
    if "undefined entity" in detail:
        causes.append("有未定義的實體參照，通常是文案裡的 & 沒有寫成 &amp;")
    if "unclosed token" in detail or "no element found" in detail:
        causes.append("檔案在中途就結束了，可能被截斷或還沒寫完")

    text = "這個檔不是合法的 XML，沒辦法解析：" + detail
    if causes:
        text += "。可能的原因：" + "；".join(causes)
    return text + "。請先把檔案修好（用 Cocos Studio 開一次、或從版本控制取回），"\
                  "這次沒有做任何改動。"


def escape_attr(value) -> bytes:
    """把一個值轉成可以放進 XML 屬性的位元組。

    換行與定位字元一定要寫成字元參照。XML 的「屬性值正規化」規定：
    屬性裡的字面換行、Enter、Tab 都會在解析時被換成**空白**——
    也就是說多行文案寫成字面換行，存進去看起來沒事，讀出來卻少了換行、
    整段擠成一行，而且不會有任何錯誤。Cocos Studio 自己也是寫 `&#xA;`。

    這裡是所有屬性值的唯一出口（節點名稱、文字內容、資源路徑都會經過），
    所以不合法字元的檢查放在這一層，二十幾個呼叫點就不必各自防一次。
    """
    text = str(value)
    bad = _ILLEGAL_XML.search(text)
    if bad:
        raise XmlValueError(
            f"值裡有 XML 不允許的字元 U+{ord(bad.group()):04X}"
            f"（在第 {bad.start()} 個字）：{text[:40]!r}。"
            "XML 1.0 的控制字元只允許 Tab、換行、Enter，其餘連寫成 &#x…; 都不合法。"
            "請把它從內容裡去掉再試。這次沒有寫入任何檔案。")
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;")
            .replace("\r", "&#xD;").replace("\n", "&#xA;")
            .replace("\t", "&#x9;")).encode("utf-8")


def unescape(value: str) -> str:
    return (value.replace("&quot;", '"').replace("&gt;", ">")
            .replace("&lt;", "<").replace("&amp;", "&"))
