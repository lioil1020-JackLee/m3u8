# M3U8 視頻下載器

一個功能強大的 M3U8 視頻下載工具，專為劇集和影片網站設計。使用現代化的 Playwright 瀏覽器自動化技術，支援智慧品質驗證、同時下載和完整的 UI 介面。

## ✨ 核心功能

### 🎯 三層智慧驗證系統
- **第一層**：快速驗證（30秒）- 檢查 M3U8 串流的解析度和可用性
- **第二層**：完整下載 - 只有通過驗證的來源才進行完整下載
- **第三層**：最終驗證 - MP4 合併後驗證實際解析度，確保 100% 真正 1080P

### 🔄 智慧重試機制
- **多來源備援**：每個集數支援多個候選來源
- **自動失敗重試**：驗證失敗時自動切換到下一個來源
- **來源優先級**：`海外推薦 > 海外 > 推薦 > 其他`

### 🚀 高性能下載
- **同時下載**：支援最多 5 個並發下載任務
- **即時處理**：找到可用來源立即開始下載
- **線程池管理**：優化的線程資源管理

### 🎨 用戶體驗
- **UI-First 設計**：友好的圖形介面，支援拖拽和右鍵選單
- **命令列支援**：完整的 CLI 參數配置
- **即時進度**：詳細的下載狀態和進度顯示
- **安全輸出**：Unicode 安全的控制台輸出

### 🛠️ 技術特色
- **現代包管理**：使用 uv 進行快速依賴管理
- **獨立打包**：支援打包為單一目錄的可執行文件
- **跨平台支援**：Windows/Linux/macOS
- **瀏覽器整合**：內建 Chromium 瀏覽器，無需額外安裝

## 📦 安裝方式

### 方式一：使用 uv（推薦）

```bash
# 安裝 uv（如果尚未安裝）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 下載專案
git clone https://github.com/lioil1020-JackLee/m3u8.git
cd m3u8

# 使用 uv 安裝依賴
uv sync

# 安裝 Playwright 瀏覽器
uv run playwright install
```

### 方式二：傳統 pip 安裝

```bash
# 下載專案
git clone https://github.com/lioil1020-JackLee/m3u8.git
cd m3u8

# 建立虛擬環境
python -m venv .venv
.\.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux/macOS

# 安裝依賴
pip install -r requirements.txt
playwright install
```

## 🚀 使用方法

### 圖形介面模式（推薦新手）

```bash
# 啟動圖形介面
uv run python m3u8.py

# 或使用打包版本
dist/m3u8/m3u8.exe
```

### 命令列模式

```bash
# 基本用法
uv run python m3u8.py --url "https://example.com/episode-page" --out-dir "./downloads"

# 自訂參數
uv run python m3u8.py \
  --url "https://example.com/episode-page" \
  --out-dir "./downloads" \
  --max-downloads 3 \
  --episode-selector ".episodes a, .playlist a" \
  --source-text "HD" \
  --wait 3.0

# Headless 模式（無視窗）
uv run python m3u8.py --url "https://example.com" --headless --no-ui
```

## ⚙️ 命令列參數

| 參數 | 說明 | 預設值 |
|------|------|--------|
| `--url` | 目標劇集頁面 URL | 無（顯示 UI） |
| `--out-dir` | MP4 輸出資料夾 | 無（顯示 UI） |
| `--max-downloads` | 同時下載數量 | 5 |
| `--episode-selector` | 集數按鈕 CSS 選擇器 | 多個備援選擇器 |
| `--source-text` | 偏好來源按鈕文字 | "FLV" |
| `--wait` | 點擊後等待秒數 | 2.0 |
| `--fast` | 啟用快速嗅探模式 | 預設啟用 |
| `--headless` | 無視窗模式 | 關閉 |
| `--no-minimize` | 不最小化瀏覽器 | 預設最小化 |
| `--no-ui` | 不顯示啟動 UI | 顯示 |

## 🏗️ 專案打包

### 使用 uv 打包 OneDir 格式

```bash
# 打包為獨立可執行文件
uv run pyinstaller --clean m3u8.spec

# 打包完成後的文件位於 dist/m3u8/
```

打包後的文件包含：
- 主執行文件 (`m3u8.exe`)
- 所有 Python 依賴
- Chromium 瀏覽器
- FFmpeg 和 N_m3u8DL-RE 工具

## 📋 系統需求

- **Python**: 3.12+
- **作業系統**: Windows 10+ / Linux / macOS
- **記憶體**: 至少 4GB RAM
- **儲存空間**: 至少 2GB 可用空間

## 🔧 專案結構

```
m3u8/
├── m3u8.py              # 主程式文件
├── m3u8.spec            # PyInstaller 配置
├── pyproject.toml       # 專案配置
├── requirements.txt     # 依賴列表
├── uv.lock             # uv 鎖定文件
├── lioil.ico           # 應用程式圖標
├── exe/                 # 外部工具
│   ├── ffmpeg.exe       # 視頻合併工具
│   └── N_m3u8DL-RE.exe  # M3U8 下載器
├── browsers/            # Playwright 瀏覽器
└── dist/                # 打包輸出目錄
```

## 🎯 支援的網站類型

- 劇集播放網站
- 影片分享平台
- 支援 M3U8 串流的任何網站
- 自訂 CSS 選擇器支援各種網站結構

## 🐛 故障排除

### 常見問題

**Q: 程式啟動失敗**
A: 確保已安裝所有依賴：`uv sync && uv run playwright install`

**Q: 下載速度慢**
A: 調整 `--max-downloads` 參數，或檢查網路連接

**Q: 無法找到集數**
A: 使用 `--episode-selector` 自訂 CSS 選擇器

**Q: 品質驗證失敗**
A: 程式會自動嘗試其他來源，請等待重試完成

### 錯誤日誌

程式會在控制台顯示詳細的處理資訊，包括：
- 驗證狀態
- 下載進度
- 錯誤訊息
- 最終結果摘要

## 📄 授權

本專案採用 MIT 授權條款。

## 🤝 貢獻

歡迎提交 Issue 和 Pull Request！

1. Fork 此專案
2. 建立功能分支 (`git checkout -b feature/AmazingFeature`)
3. 提交變更 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 開啟 Pull Request

## 📞 聯絡方式

- **作者**: lioil1020-JackLee
- **GitHub**: [https://github.com/lioil1020-JackLee/m3u8](https://github.com/lioil1020-JackLee/m3u8)

---

**注意**: 本工具僅供學習和個人使用，請遵守相關網站的使用條款和版權法規。