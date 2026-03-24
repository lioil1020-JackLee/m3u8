# M3U8 視頻下載器

一個以 **Playwright + N_m3u8DL-RE + FFmpeg** 為核心的 M3U8 劇集下載工具。  
目前提供 GUI 與 CLI 兩種使用方式，支援集數範圍選取、並行下載、下載後解析度檢查、低畫質過濾清理，以及「未完成集數自動回填重跑」流程。

## 目錄

- [功能總覽](#功能總覽)
- [系統需求](#系統需求)
- [快速開始](#快速開始)
- [GUI 使用說明](#gui-使用說明)
- [CLI 使用說明](#cli-使用說明)
- [集數選擇語法](#集數選擇語法)
- [下載流程說明](#下載流程說明)
- [下載完成後行為](#下載完成後行為)
- [暫存與輸出規則](#暫存與輸出規則)
- [故障排除](#故障排除)
- [打包與發佈](#打包與發佈)
- [專案結構](#專案結構)

## 功能總覽

### 核心能力

- 自動開啟目標頁面，嗅探 `.m3u8` 請求並去重。
- 以 FLV 來源索引選擇播放來源。
- 劇集掃描與下載/合併/檢查採流水線並行處理。
- 支援 `N_m3u8DL-RE` 下載 TS 片段，再用 `FFmpeg` 合併 MP4。
- 下載後自動檢查解析度（優先 `ffprobe`，後備 `ffmpeg -i`）。
- 產生 `重新下載.txt` 報告（失敗或低解析度集數）。

### 集數控制

- 支援單集、範圍、混合輸入（例如 `1-5,8,10-12`）。
- 自動檢查輸入集數是否超出頁面實際集數範圍。
- 對重複集數文字做後綴編號（如 `E153`, `E153-2`）。
- 非數字集數（特別篇）會歸到 `S000`。

### 品質與清理

- 預設啟用低解析度過濾（寬度 `< 1920`）。
- 被判定低解析度的 MP4，會在報告後自動刪除（僅刪成功下載但寬度不足的檔案）。
- 下載結束會清理暫存資料夾。

### 互動重試流程（最新版）

- 若存在「需要重新下載的集數」：
  - 終端顯示 `y/n`：「是否結束程式？」
  - 輸入 `y`：結束程式
  - 輸入 `n`：自動重開設定畫面，並帶入上次設定
- 重開設定時，會帶入上次：
  - URL
  - FLV source
  - Output folder
  - Temp folder
  - filter 選項
- 只有 `Episodes` 會改為「上一次未完成清單」
- 若本次全部成功（無未完成清單），程式會直接結束，不再停在 `Enter`。

## 系統需求

### 執行環境

- Python `>=3.12`（參考 `pyproject.toml`）
- Windows（目前外部工具使用 `.exe`，主流程以 Windows 工具鏈為主）

### Python 套件

- `playwright`
- `requests`
- 其他依賴請見 `requirements.txt`

### 外部工具（必需）

請確認下列檔案位於專案的 `exe/` 目錄：

- `exe/N_m3u8DL-RE.exe`
- `exe/ffmpeg.exe`
- `exe/ffprobe.exe`

若缺少任一工具，下載/合併/解析度檢查會失敗或降級。

## 快速開始

### 1) 安裝依賴

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### 2) 執行（GUI）

```bash
python m3u8.py
```

### 3) 執行（CLI）

```bash
python m3u8.py --no-ui --url "https://example.com/series" --out-dir "F:/videos"
```

## GUI 使用說明

啟動後會顯示設定視窗：

- `Target page URL`：目標頁面網址
- `FLV source`：FLV 來源索引（預設 `1`）
- `Episodes`：要下載的集數語法（預設 `.` = 全部）
- `Output folder (for MP4)`：MP4 輸出資料夾
- `Temp folder`：暫存根目錄
- `過濾低分辨率 (<1920寬)`：是否啟用低解析度過濾（預設啟用）

補充：

- 文字框支援右鍵貼上。
- 關閉 GUI 且未填 URL 時，程式會回退到終端要求輸入 URL。
- 當你在「未完成重跑」流程選擇 `n` 時，會自動重開此視窗並預填上次資料。

## CLI 使用說明

### 基本範例

```bash
python m3u8.py \
  --url "https://example.com/series" \
  --flv-idx 1 \
  --start-ep "." \
  --out-dir "F:/videos" \
  --tmp-root "R:/" \
  --max-downloads 5 \
  --no-ui
```

### 參數一覽

| 參數 | 預設值 | 說明 |
|---|---:|---|
| `--url` | `None` | 目標頁面 URL |
| `--out-dir` | `None` | 輸出資料夾（未給時使用當前目錄） |
| `--flv-idx` | `1` | FLV 來源索引 |
| `--start-ep` | `.` | 集數選擇語法 |
| `--max-downloads` | `5` | 並發工作線程數 |
| `--wait` | `2.0` | M3U8 嗅探等待秒數（目前程式主流程未直接使用此參數） |
| `--no-ui` | `False` | 不顯示 GUI |
| `--tmp-root` | `R:/` | 暫存根目錄 |
| `--ram-tmp` | `True` | 優先使用記憶體暫存（若 `tmp-root` 未指定） |
| `--no-ram-tmp` | - | 關閉記憶體暫存優先 |
| `--filter-resolution` | `True` | 啟用低解析度過濾 |
| `--no-filter-resolution` | - | 關閉低解析度過濾 |
| `--sync-fix` | `True` | 啟用 FFmpeg 音訊同步修正 |
| `--no-sync-fix` | - | 停用同步修正（較快但可能影音不同步） |

## 集數選擇語法

`--start-ep` / GUI 的 `Episodes` 支援：

- `.` 或空字串：全部集數
- `1`：只下載第 1 集
- `1-10`：下載第 1 到第 10 集
- `1,5,9,15`：下載指定多集
- `1-5,8,10-12`：混合模式

範例：

```bash
python m3u8.py --no-ui --url "https://example.com" --start-ep "1-5,8,10-12"
```

## 下載流程說明

### 1) 頁面分析

- 啟動 Playwright Chromium（headless）。
- 阻擋 `image/stylesheet/font/media` 以加速。
- 尋找 `.jujiepisodios` 容器與 `FLV` 按鈕。
- 依 `flv-idx` 對應容器。

### 2) 劇集編號整理

- 讀取每個集數按鈕文字。
- 純數字視為正篇（`S001.Exxx`）。
- 非數字視為特別篇（`S000.Exxx`）。
- 相同集數文字會加後綴（如 `-2`）。

### 3) 嗅探與排程

- 對每集點擊後監聽 request，收集候選 `.m3u8`。
- 使用 URL 正規化規則去重（忽略 query、統一 `/play/hls/`）。
- 生產者（主線程）掃描，消費者（多線程）下載/合併/檢查。

### 4) 下載與合併

- 下載：`N_m3u8DL-RE --skip-merge --tmp-dir ...`
- 合併：優先使用 `raw.m3u8` / `index.m3u8`，否則走 `concat.txt`。
- `sync-fix` 開啟時會加入音訊同步修正參數。

### 5) 解析度檢查

- 優先 `ffprobe` 讀取 `width,height`。
- 失敗時用 `ffmpeg -i` 解析後備。

## 下載完成後行為

程式在每次下載結束會：

1. 顯示成功統計與解析度報告
2. 產生 `重新下載.txt`
3. 計算 `需要重新下載的集數`
4. 若啟用過濾，刪除寬度 `<1920` 的 MP4
5. 清理暫存資料夾

### 未完成重跑互動

當 `需要重新下載的集數` 非空時：

```text
偵測到不合格檔案清單，是否結束程式？[y/n]
```

- `y`：結束
- `n`：重開設定畫面，並自動帶入上次設定；`Episodes` 改填未完成清單

### 全部成功時

- 若沒有未完成清單，程式直接結束（不再等待 `Enter`）。

## 暫存與輸出規則

### 命名規則

輸出 MP4 檔名格式：

- 正篇：`{show_name}.S{season:03d}.E{episode:03d}{suffix}.mp4`
- 特別篇：`{show_name}.S000.E{episode:03d}.mp4`

`show_name` 來自頁面標題，非法字元會被替換為 `_`。

### 暫存目錄解析順序

`resolve_tmp_root()` 的優先序：

1. 使用者指定 `--tmp-root`（GUI Temp folder 也會回填到這裡）
2. 若啟用 `ram_tmp`：
   - 環境變數 `M3U8_RAM_TMP`
   - Linux `/dev/shm`
3. 回退到 `out_dir/nm3_tmp`

### 報告檔

- `重新下載.txt`：包含完成統計、每集解析度與重下載清單

## 故障排除

### 找不到下載器

症狀：顯示 `下載器不存在`  
處理：確認 `exe/N_m3u8DL-RE.exe` 存在。

### 找不到集數容器

症狀：顯示 `找不到集數容器`  
處理：

- 確認網址正確
- 確認網站結構未變更
- 確認頁面可正常載入

### 合併失敗 / 解析度 Unknown

處理：

- 檢查 `exe/ffmpeg.exe`、`exe/ffprobe.exe`
- 檢查暫存是否完整
- 先關閉過濾觀察原始結果：`--no-filter-resolution`

### 集數超範圍

症狀：`輸入的集數超出範圍`  
處理：改成站內實際存在的集數區間。

## 打包與發佈

專案提供 `m3u8.spec`，可用 PyInstaller 打包：

```bash
uv run pyinstaller --clean m3u8.spec
```

產物通常在 `dist/` 目錄。

## 專案結構

```text
m3u8/
├─ m3u8.py
├─ README.md
├─ requirements.txt
├─ pyproject.toml
├─ m3u8.spec
├─ exe/
│  ├─ N_m3u8DL-RE.exe
│  ├─ ffmpeg.exe
│  └─ ffprobe.exe
└─ browsers/
```

---
