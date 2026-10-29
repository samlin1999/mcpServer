# csdmcp — 直接編輯 Cocos Studio `.csd` 的 MCP 伺服器

讓 AI 助理能**改**（不只是看）Cocos Studio 的 `.csd`：改屬性、增刪節點、搬移階層、
從零建立新檔，並且立刻用 [csdview](../../CsdAiViewer) 渲染出畫面確認結果。

檢視功能全部轉呼叫 csdview 的 CLI，行為與各專案 `CLAUDE.md` 裡寫的判讀準則完全一致；
編輯功能是這裡自己實作的。

---

## 安裝

需要 Python 3.9+，以及已經裝好的 csdview（在 CsdAiViewer 資料夾執行 `pip install -e .`）。

```bat
安裝.bat
```

會同時註冊到 **Claude Code** 與 **Cursor**（使用者層級，所有專案都用得到）。
`python.exe` 與 `run.py` 的路徑由安裝程式自動搜尋（正在跑的解譯器 → PATH 上的
`python` / `python3` → Windows 的 `py -3`），**不必手填**。Cursor 那邊寫進
`~/.cursor/mcp.json`（Windows 為 `%USERPROFILE%\.cursor\mcp.json`），
既有的其他 MCP 伺服器會保留。安裝過程會印出實際用到的 Python 路徑。

若只想看搜到的設定、先不寫檔：

```bat
python install.py --show-config
```

Claude Code 也可以手動註冊，路徑一樣由上面那條指令印出來，或直接：

```bash
claude mcp add --scope user csd -- <python.exe> <這個資料夾>\run.py
claude mcp list          # 應該看到 csd: ... ✓ Connected
```

然後打開 Cursor Settings → Customize → MCPs，確認 `csd` 已連線；沒出現就重開 Cursor。

## 要對哪個專案動手

**沒有寫死任何專案路徑。** 認得的專案來源依序是：

1. 呼叫時直接給的絕對路徑
2. 呼叫時給的 `project` 參數
3. 環境變數 `CSDMCP_ROOTS`（用 `;` 分隔的額外 cocosstudio 根目錄）
4. **csdview 自己的登錄檔** `~/.csdview_projects`——在 csdview 設定過的專案這裡自動就認得
   （路徑可用 `CSDVIEW_PROJECTS` 環境變數覆寫，csdview 與這裡認同一個變數）
5. 當前工作目錄往上找 `cocosstudio`

換到新專案：先 `list_projects` 看現況，再用 `add_project` 登錄（會寫進同一份登錄檔，
csdview 那邊也跟著認得）。同一個檔名在多個專案裡都有時**一律要求指定**，不會替你猜。

---

## 工具

### 專案
| 工具 | 用途 |
| --- | --- |
| `list_projects` | 認得哪些專案、各自的 cocosstudio 根目錄 |
| `add_project` | 登錄一個新專案 |
| `list_csd` | 列出專案底下的 `.csd` |

### 檢視（轉呼叫 csdview）
| 工具 | 用途 |
| --- | --- |
| `info` | 動畫清單、設計解析度、引用的其他 `.csd` |
| `tree` | 節點階層與畫面位置——**編輯前先用它取得正確的節點路徑** |
| `find` | 依名稱或型別搜尋節點 |
| `spec` | 每個節點的座標、尺寸、貼圖、文字 |
| `doctor` | 缺失資源、`staleLayout`、`textOverflow` |
| `render` | 渲染一格，**圖片直接回傳**，不必先寫檔再讀檔 |
| `compare` | 與 Cocos Creator 截圖比對 |

### 建立
| 工具 | 用途 |
| --- | --- |
| `create_csd` | 從零建立新的 `.csd`，可用 `nodes` 一次把內容也建好，也可用 `anim` 接著寫時間軸 |
| `register_csd` | 把既有 `.csd` 登錄進 `.ccs`（或取消登錄） |
| `list_anim` | 讀時間軸（哪個節點、哪條屬性、哪些關鍵影格） |
| `set_anim` | 寫時間軸；`frames` 為空就刪掉那條軌道 |

### 編輯
| 工具 | 用途 |
| --- | --- |
| `set_prop` | 改屬性 |
| `add_node` / `delete_node` / `duplicate_node` / `reparent` | 結構調整 |
| `edit_batch` | 一次做多個編輯，全部成功才寫檔 |
| `history` / `undo` | 看改過什麼、退回上一步 |

編輯類工具都吃 `preview: true`，存檔後直接回傳一張渲染圖——改完馬上看得到結果。

### 動畫

Cocos Studio 的動畫是屬性軌道：每個節點的每個屬性一條，裡面是關鍵影格。
靜態屬性是第 0 格的底，時間軸是覆蓋層。`create_csd` 不會憑空長出時間軸，要另外寫。

```json
set_anim({
  "csd": "voucherCenter/test.csd",
  "node": "Node/Sprite_2",
  "tracks": {
    "position": [
      {"frame": 0, "x": 0, "y": -15, "easing": 5},
      {"frame": 9, "y": -6, "easing": 2},
      {"frame": 18, "y": -15}
    ],
    "scale": [
      {"frame": 0, "scaleX": 1.05, "scaleY": 1.05},
      {"frame": 9, "scaleY": 1.15}
    ]
  },
  "duration": 75,
  "preview": true,
  "frame": 9
})
```

屬性：`position` `scale` `rotation` `anchor` `alpha` `color` `visible` `image` `blend` `event` `inner`。
省略的座標從上一格（或節點目前的值）接著用。`easing` 給 0–29 或 `quadOut` 這種名稱。
貼圖序列把 `tween` 預設關掉，並依有沒有 `plist` 寫成 MarkedSubImage / Normal。
`Duration` 自動等於最晚的關鍵影格；要留空白尾巴再傳 `duration`。
具名片段用 `clips: [{name, start, end}]`。寫之前用 `list_anim` 看現況。

### 可以改的屬性

位置尺寸 `x` `y` `w` `h`、相對位移 `dx` `dy`、錨點 `anchorX` `anchorY`、
縮放 `scaleX` `scaleY`、旋轉 `rotation`、百分比 `preX` `preY` `preW` `preH`、
外觀 `alpha`(0-255) `visible` `color`(`#RRGGBB`) `colorAlpha` `flipX` `flipY`、
內容 `text` `fontSize` `image` `plist` `pressedImage` `disabledImage`、
其他 `name` `tag` `zOrder` `touchEnabled` `clipAble` `blendAdditive`。

樣板沒涵蓋的欄位用 `attrs`（節點屬性）或 `elements`（子元素屬性），值給 `null` 代表刪掉。
**不認得的屬性一律報錯，不會靜默忽略。**

---

## 這套東西在小心什麼

寫這個工具真正的難處不是改 XML，而是三件不小心就會把檔案弄壞、而且**當下不會有任何錯誤訊息**的事。

### 1. 位置資料存了三份，改一份就壞了

Cocos Studio 把同一個位置同時存成絕對值（`Position`/`Size`）、對父層的百分比
（`PrePosition`/`PreSize`）、以及四個邊界（`LeftMargin` 等）。只改絕對值，
檔案照樣開得起來、csdview 照樣畫得出來，但**移植到引擎後位置會跟編輯器裡看到的不一樣**。

所以每個動到座標的操作都會回頭重算全部欄位；改尺寸還會連帶重算直接子節點的
（它們的父層尺寸變了）。csdview 的 `doctor` 會把這種不一致報成 `staleLayout`，
測試就是直接拿它來驗收。

兩個容易寫錯的細節：邊界用的是**未套用 Scale 的尺寸**；父層某一軸是 0 時，
該軸的百分比欄位無從計算，編輯器直接不寫。

### 2. 整份重寫 XML 會產生沒人審得動的 diff

`.csd` 都在 git 裡。用 ElementTree 讀進來再寫回去，縮排、屬性順序、CRLF、
空元素寫法全部會變。所以這裡記下每個元素與屬性在**原始位元組**裡的位置，
只置換那一小段。實測「移動一個節點並改寬度」在 957 行的檔案裡只動 4 行。

連新屬性的插入順序都照 Cocos 的慣例（`X` 在 `Y` 前、`ctype` 一律最後），
省略預設值的寫法也跟著原檔——目標是產生的檔案跟編輯器存出來的分不出來。

### 3. 新建的 `.csd` 沒登錄進 `.ccs` 等於白做

`.ccs` 專案檔裡有一份完整的資源樹。檔案沒登錄進去，用 Cocos Studio 開專案時
資源列表就是看不到它。`create_csd` 預設會一併登錄，沿路缺的資料夾也補上。

### 4. 同一個檔案不只我們在寫

每個 Claude Code／Cursor 視窗都是一個獨立的伺服器行程，而使用者往往還開著 Cocos Studio。
沒有防護的話，兩邊的「讀進來 → 改 → 寫回去」會互相蓋掉，**而且完全沒有痕跡**。

- **`.csd` 走樂觀檢查**：開檔時記下修改時間與大小，存檔前比對，不一致就中止並要求重讀。
  這裡不能用鎖——真正的對手是 Cocos Studio，它不會理會我們的鎖。
  （時間戳變了但內容其實一樣的情況會再比一次內容，不會誤擋。）
- **`.ccs` 與快照紀錄走檔案鎖**（`locking.py`），因為那些檔案只有這個工具會寫；
  `.ccs` 另外再加一道修改時間檢查，防編輯器自己存檔。
- **建新檔用 `O_CREAT|O_EXCL`**，讓檔案系統決定誰搶到，而不是「先檢查再寫」。
- **快照序號同樣用 `O_EXCL` 搶佔**，紀錄檔原子寫入——不然中斷會留下半截 JSON，
  而讀取端把解析錯誤當成「沒有紀錄」，等於整份還原歷史靜默消失。

### 其他

- `ActionTag` 是時間軸用來認節點的鍵，全檔唯一。複製節點一定要換一組新的，
  否則兩個節點會被同一段動畫驅動；刪節點則要一併清掉指向它的時間軸。
- 每次寫檔前先存快照（放在系統暫存區，不污染專案），`undo` 可以退回。
- 寫檔前會先讓 csdview 解析一遍，解析不過就放棄寫入、原檔不動。
- `edit_batch` 是一筆交易：中途任何一步失敗就整批放棄。
- 節點路徑與 csdview 的 `tree`／`find` 完全一致。同層有同名節點時要加 `[n]` 序號
  （csdview 不會輸出序號，所以照抄路徑會撞到歧義，這時工具會列出候選要求指定）。

---

## 效能上的幾個關鍵決定

| 決定 | 為什麼 |
| --- | --- |
| **一筆編輯只 splice 一次** | 每次 splice 都要重新解析整份檔案（540 KB 要 55 ms）。所有 cuts 收齊後一次套用，改尺寸連帶重算 28 個子節點從 88 次重建／578 ms 降到 1 次／8.5 ms |
| **子元素不存在就直接組出整行** | 不走「先建空元素、再設屬性」，省掉每個子元素一次重建 |
| **csdview 在行程內呼叫** | `csdview.cli.main(argv)` 跟子行程走同一條程式碼、行為零差異，但省下每次約 240 ms 的啟動成本（`info` 從 276 ms 降到 7 ms）。stdout 一定要先導開，那是 MCP 的協定通道 |
| **`@ActionTag` 查詢走一趟建表** | 原本對每個節點各呼叫一次 `elem_of`，540 KB 檔要 308 ms，現在 2.6 ms |
| **目錄索引快取 20 秒** | 掃一個 1500 檔的專案要 137 ms。有效期短是刻意的：檔案可能被別的程式新增 |
| **渲染輸出用 sha1 命名並定期清理** | 內建的 `hash()` 每個行程都不一樣（`PYTHONHASHSEED` 隨機化），跨行程既無法重用、暫存檔又只增不減 |

---

## 測試

```bash
python tests/test_editor.py       # 編輯核心，拿專案裡的真實 .csd 當素材
python tests/test_protocol.py     # 開子行程走 stdin/stdout，驗協定層
python tests/test_create.py       # 建檔與 .ccs 登錄（在暫存目錄做，不碰真專案）
python tests/test_anim.py         # 時間軸讀寫（在暫存目錄做）
python tests/test_concurrency.py  # 並行編輯、建檔、寫快照
```

另外 `test_anim.py` 釘住時間軸讀寫（關鍵影格、easing、貼圖序列、Duration）。

兩個刻意的設計：測試會挑「有帶尺寸的中間層」的素材，
否則「改尺寸要重算子節點」與「搬移階層」這兩條最容易寫錯的路徑根本走不到；
並行那份驗的則全是「不修就會靜默吃掉使用者工作」的路徑——那類問題不會拋例外、
不留痕跡，只能靠測試釘住。

---

## 檔案

| 檔案 | 負責 |
| --- | --- |
| `csdmcp/xmldoc.py` | 位元組層級的 XML 索引與置換 |
| `csdmcp/layout.py` | 冗餘版面欄位換算、跨階層的座標變換 |
| `csdmcp/editor.py` | 節點層級的編輯操作、建立新檔 |
| `csdmcp/ccs.py` | `.ccs` 專案檔的資源樹登錄 |
| `csdmcp/projects.py` | 專案與 `.csd` 的路徑解析 |
| `csdmcp/history.py` | 快照與還原 |
| `csdmcp/locking.py` | 跨行程檔案鎖（給 `.ccs` 與快照紀錄用） |
| `csdmcp/view.py` | 轉呼叫 csdview、圖片內嵌回傳 |
| `csdmcp/anim.py` | 時間軸讀寫（`set_anim` / `list_anim`） |
| `csdmcp/tools.py` | MCP 工具定義與實作 |
| `csdmcp/server.py` | MCP stdio 協定（零相依） |
| `run.py` | 伺服器啟動點（絕對路徑呼叫，不必先安裝套件） |
| `install.py` | 安裝流程（註冊 Claude Code 與 Cursor）；`安裝.bat` 只是它的 ASCII 外殼 |

### 為什麼安裝訊息寫在 Python 而不是 `.bat`

cmd.exe 是一邊執行、一邊按**位元組偏移**重讀批次檔的。批次檔裡有中文、
又在中途 `chcp` 切編碼的話，它的解析位置會整個錯位，接下來的 `echo` 文字
就被當成指令送去執行（畫面上是一串「不是內部或外部命令」）。

所以 `安裝.bat` 只留純 ASCII 的幾行，訊息一律由 `install.py` 印；
編碼也在 Python 行程裡切（`SetConsoleOutputCP(65001)` 加上 stdout 改成 UTF-8，
兩件事要一起做，只做一半照樣是亂碼），那時批次檔的解析早就結束了。
