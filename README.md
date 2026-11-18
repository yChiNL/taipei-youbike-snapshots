# Taipei YouBike 2.0 / 2.0E Shared-Dock Snapshots (`整點駐留批次型`)

自動收集臺北市公共自行車（`area_code == "00"`，約 1,800+ 站）之**一般車（`yb2` / YouBike 2.0）**與**電輔車（`eyb` / YouBike 2.0E）**共用車柱即時狀態快照，供碩士論文驗證雙車種車柱斷貨／塞柱預警與夜間貨車調度實況使用。

---

## 一、排程架構：整點駐留批次型（Hourly Resident Batch）

- **無須開啟個人電腦**：全天候由 GitHub Actions 雲端主機自動執行（**務必將 GitHub 倉庫設為 `Public` 公開倉庫**，以享有免費無上限之 Actions 運算分鐘數；本倉庫僅儲存政府公開車柱統計數字，不含任何論文內文或機密）。
- **每 10 分鐘精準取樣、每 1 小時打包上傳 1 次**：
  1. GitHub Actions 每小時整點（`cron: "0 * * * *"`）喚醒一次虛擬機。
  2. `collect_snapshot.py --resident-hour` 啟動後立即抓第 1 筆，接著自動對齊時鐘於 **`:10:05`、`:20:05`、`:30:05`、`:40:05`、`:50:05`** 各抓 1 筆（每小時共 6 筆，避開 GitHub 排程誤點問題）。
  3. 抓完 `:50` 該筆後（早於 `:54` 安全截止線），腳本自動結束並由 GitHub Actions **一次打包 Commit & Push**，留約 9 分鐘緩衝迎接下一個整點班次（一天僅 24 筆清爽 Commit）。
- **零外部套件依賴**：僅使用 Python 3.12 內建標準庫（`urllib`、`gzip`、`csv`、`zoneinfo`），免 `pip install`，開機 2 秒內即開始抓資料。

---

## 二、資料目錄與欄位格式

```text
taipei-youbike-snapshots/
├── .github/workflows/collect.yml   # GitHub Actions 整點駐留排程設定
├── collect_snapshot.py             # 核心收集腳本（支援 --once 與 --resident-hour）
└── data/
    ├── collection_status.json      # 最新一筆收集心跳摘要（網頁上可直接查看是否正常運作）
    ├── stations_meta.csv           # 站點靜態基本資料表（自動維護，有新站或改名自動補入）
    └── snapshots/
        └── YYYY-MM-DD.csv.gz       # 依臺北時間（UTC+8）每日 1 檔之 gzip 壓縮表格（約 1.8 MB/天）
```

### 1. `data/stations_meta.csv`（站點靜態表）
| 欄位 | 說明 | 範例 |
|---|---|---|
| `station_no` | 官方站點唯一代號（串接主鍵） | `500101001` |
| `name_tw` | 正規化中文站名 | `捷運科技大樓站` |
| `district_tw` | 臺北市行政區 | `大安區` |
| `address_tw` | 站點地址 | `復興南路二段235號前` |
| `lat`, `lng` | WGS84 經緯度 | `25.02605`, `121.5436` |
| `latest_capacity` | 最新觀測總車柱數 | `28` |
| `first_seen_date`, `last_seen_date` | 首次／最近觀測日期 | `2026-11-18` |

### 2. `data/snapshots/YYYY-MM-DD.csv.gz`（每 10 分鐘動態車柱表）
| 欄位 | 說明 | 範例 |
|---|---|---|
| `slot_time` | 對齊後之 10 分鐘臺北時間刻度 | `2026-11-18 20:30:00` |
| `fetched_at` | 實際抓取臺北時間 | `2026-11-18 20:30:05` |
| `src_updated_at` | 官方車柱回報更新時間 | `2026-11-18 20:29:32` |
| `station_no` | 官方站點代號 | `500101001` |
| `status` | 營運狀態（`1`=正常營運，其餘為暫停） | `1` |
| `capacity` | 當下總車柱數（`parking_spaces`） | `28` |
| `yb2` | 在站 **YouBike 2.0 一般白車**數量 | `22` |
| `eyb` | 在站 **YouBike 2.0E 橘色電輔車**數量 | `2` |
| `empty` | 剩餘可歸還空柱數（`empty_spaces`） | `4` |
| `forbidden` | 故障／禁用車柱數（`forbidden_spaces`） | `0` |

---

## 三、首次上線設定步驟（3 分鐘完成）

1. **在 GitHub 網頁新增公開倉庫**：
   前往 GitHub 右上角 **`+` → `New repository`**：
   - Repository name 輸入：`taipei-youbike-snapshots`
   - Visibility **務必選 `Public`**（公開倉庫 Actions 排程才免費無上限！）
   - **不要**勾選 Add README / .gitignore（保持空白倉庫），點 **`Create repository`**。

2. **在本機終端機把這個資料夾推上去**：
   ```bash
   cd /Users/chin/Documents/Programming/taipei-youbike-snapshots
   git add .
   git commit -m "chore: init Taipei YouBike hourly resident snapshot collector"
   git branch -M main
   git remote add origin https://github.com/<你的GitHub帳號>/taipei-youbike-snapshots.git
   git push -u origin main
   ```

3. **到 GitHub 網頁按一次「10 秒手動測試」確認綠燈**：
   - 點進該 GitHub 倉庫上方的 **`Actions`** 分頁。
   - 點左側 **`Collect Taipei YouBike 2.0/2.0E Snapshots (Hourly Resident Batch)`**。
   - 點右側 **`Run workflow`** 下拉按鈕 → `mode` 保持預設 **`once`** → 按綠色的 **`Run workflow`**。
   - 等約 **10～15 秒**看到綠色勾勾 ✅，回到倉庫首頁點進 `data/`，只要看到 `stations_meta.csv`、`collection_status.json` 與 `snapshots/YYYY-MM-DD.csv.gz` 出現，就代表大功告成！之後每個整點都會全自動駐留收集。

> **小提醒**：GitHub 規定若倉庫連續 **60 天**完全沒有人互動，會自動暫停排程並寄信提醒；只要兩個月內隨意點一次手動 `Run workflow` 或在網頁介面按一下 `Enable workflow` 即可持續運作。

---

## 四、未來論文實驗讀取範例（Python `pandas` 免解壓直接讀）

```python
import glob
import pandas as pd

meta = pd.read_csv("data/stations_meta.csv", dtype={"station_no": str})
files = sorted(glob.glob("data/snapshots/*.csv.gz"))
snaps = pd.concat((pd.read_csv(f, dtype={"station_no": str}) for f in files), ignore_index=True)

df = snaps.merge(meta[["station_no", "name_tw", "district_tw", "lat", "lng"]], on="station_no", how="left")
print(df.head())
```
