# Model packaging and API endpoint setup
_Exported on 2026/3/11 at 18:54:51 [GMT+8] from Cursor (2.6.13)_

---

**User**

My boss requests us to package the trained model and expose interface via API endpoint. We are given a document @doc/ML_API_PROTOCOL.md to follow. Can you check what do we need to do to do so?

---

**Cursor**


正在閱讀 ML API 協定文件，並檢查專案現況。



檢查現有 API server 實作與協定要求的差異。



繼續查看 `get_alerts` 的實作與回應格式，並確認 server 的啟動 port。



檢查 `alerts` 與 `validation_results` 的資料庫欄位是否與協定一致。



檢查 scorer 的 Phase-1 欄位與 validation_results 的完整欄位：



已對照 `doc/ML_API_PROTOCOL.md` 與現有實作，整理出要滿足協定需要做的事項如下。

---

## 依 `ML_API_PROTOCOL.md` 需要做的事

文件是給 **Dashboard 團隊 (wa1)** 用的：ML 服務提供 **兩個 GET**（`/alerts`、`/validation`），Dashboard 用輪詢取資料。  
**沒有**要求包裝「訓練好的模型」或提供 `/score` 推論 API；協定只定義 **alerts 與 validation 的對外介面**。

---

### 1. 端點路徑與 Port

| 協定要求 | 目前實作 | 需調整 |
|----------|----------|--------|
| **Port** `8001` | `api_server.py` 用 **8000**（約第 289 行） | 改為 **8001** |
| **GET `/alerts`** | **GET `/get_alerts`** | 改為 **`/alerts`**（或另加一條 route 指向同一邏輯） |
| **GET `/validation`** | **GET `/get_validation`** | 改為 **`/validation`**（同上） |

Dashboard 會打 `http://localhost:8001/alerts` 和 `http://localhost:8001/validation`，所以 **port 與 path 必須一致**。

---

### 2. `GET /alerts` 行為與回應格式

**Query 參數：**

- `ts`（可選）：只回傳 **晚於** 此時間的 alerts。  
  現有 `get_alerts` 已有 `ts` 過濾，這部分符合。
- `limit`（可選）：**未帶 `ts` 時**，最多回傳筆數。  
  目前 **沒有** 實作 `limit`，需補上。
- **兩者都沒給**：協定要求回傳 **過去 24 小時**。  
  目前是回傳 **全部**，需改為：無 `ts` 且無 `limit` 時，預設只取最近 24 小時。

**Response 欄位（協定規定只露出這些）：**

- `bet_id`, `ts`, `bet_ts`, `player_id`, **`casino_player_id`**, `table_id`, `position_idx`, `session_id`, `visit_avg_bet`, **`is_known_player`**

目前實作是 `SELECT * FROM alerts` 後整表丟出，會多出很多欄位（如 `loss_streak`, `score`, `reason_codes` 等）。需要改為：

- **只回傳** 上列協定欄位。
- **`casino_player_id`**：協定保留，目前可固定回傳 `null`（若 DB 沒有此欄就別從 DB 讀，直接加在 response）。
- **`is_known_player`**：協定表示「是否為已知/被追蹤玩家」。DB 有 **`is_rated_obs`**（scorer Phase-1），語意接近，可對應為 `is_known_player`（例如 `is_rated_obs == 1` → `1`，否則 `0`）。若沒有 `is_rated_obs` 就回傳 `1`/`0` 的合理預設即可。

另外協定註明：**NaN/Inf 要正規化成 `null`**。你已有 `replace({np.nan: None, ...})`，這點保持即可。

---

### 3. `GET /validation` 行為與回應格式

**Query 參數：**

- `ts`：只回傳 **驗證時間晚於** 此時間的結果。  
  目前用 `validated_at > ts` 過濾，符合。
- `bet_id` / `bet_ids`：單筆或逗號分隔多筆。  
  已有，符合。
- **全部沒給**：協定要求回傳 **過去 24 小時**。  
  目前是回傳 **全部**，需改為：無任何參數時，預設只取 **最近 24 小時**（例如 `validated_at > now - 24h`）。

**Response 欄位：**

- `ts`, `player_id`, **`casino_player_id`**, `bet_id`, `walkaway_ts`, `TP`, `sync_ts`, `reason`, `bet_ts`

目前 `get_validation` 已做欄位 renames（`alert_ts`→`ts`, `gap_start`→`walkaway_ts`, `result`→`TP`, `validated_at`→`sync_ts`），但：

- **`casino_player_id`**：協定保留，目前回應裡沒有。需在輸出中加上此欄，值可先固定 `null`（若 DB 的 validation_results 沒有，就別從 DB 讀，在組 response 時補上）。
- **`TP` 型別**：協定範例是字串 `"TP"`。DB 的 `result` 是 0/1（validator 存 `int(bool(r.result))`）。若 Dashboard 預期字串，需在 API 層做對應（例如 1→`"TP"`, 0→`"FP"` 或 `"MISS"`，要與 wa1 確認）。

同樣，NaN/Inf → `null` 已有處理即可。

---

### 4. 其他協定要求（已符合或易確認）

- **Content-Type**：`application/json` — Flask `jsonify` 預設即為 JSON。
- **CORS**：協定要求 fully open（`*`）。你已在兩個 endpoint 設 `resp.headers["Access-Control-Allow-Origin"] = "*"`，符合。
- **時間**：全部 HK time (`+08:00`)。你已用 `HK_TZ` 做 format，符合。
- **Base URL**：`http://localhost:8001` — 改 port 後即符合。

---

### 5. 「包裝訓練好的模型」與 API 的關係

- **`ML_API_PROTOCOL.md` 只定義「alerts + validation」的 API**，沒有定義模型推論（例如 `/score`）。  
  因此，要滿足**這份文件**，只需要：  
  - 把 **已由 scorer 寫入 DB 的 alerts** 與 **validator 寫入的 validation_results**，透過 **port 8001** 的 **`/alerts`** 和 **`/validation`** 照協定格式暴露出去即可。  
- 若老闆另外有說「要把訓練好的模型包起來並用 API 暴露」，那通常是指：  
  - 模型檔（如 `model.pkl`）打包 + 一個 ** scoring API**（例如 POST `/score`）。  
  那份需求在 repo 裡有另一份 `doc/model_api_protocol_v2.md` 的範例（Flask + `/score`）；與 **`ML_API_PROTOCOL.md`** 是兩件事。  
- 實務上：  
  - **先滿足 ML_API_PROTOCOL.md**：改 port、路徑、預設 24h、`limit`、回應欄位（含 `casino_player_id`、`is_known_player`、必要時 `TP` 字串化）。  
  - **若還要「模型 API」**：再依 `model_api_protocol_v2.md` 或內部共識，另做一個服務或同一 app 下加 `/score`。

---

## 檢查清單（對照 ML_API_PROTOCOL.md）

1. **Port**：`api_server` 改為 **8001**。  
2. **路徑**：提供 **GET `/alerts`** 與 **GET `/validation`**（可改現有 route 或加一層轉發）。  
3. **`/alerts`**  
   - 無參數時：只回傳 **最近 24 小時**。  
   - 支援 **`limit`**（無 `ts` 時使用）。  
   - 回應**只含**協定欄位，並包含 **`casino_player_id`**（可 `null`）、**`is_known_player`**（可對應 `is_rated_obs`）。  
4. **`/validation`**  
   - 無參數時：只回傳 **最近 24 小時**。  
   - 回應包含 **`casino_player_id`**（可 `null`）；**`TP`** 若協定要字串再轉換。  
5. **CORS / JSON / 時區**：維持現狀即可。  
6. **若另有「模型包裝 + 推論 API」需求**：再依 `model_api_protocol_v2.md` 或內部規格實作，與本協定分開處理。

以上是依 `doc/ML_API_PROTOCOL.md` 檢查後，要達成協定所需做的事項；若你貼出 `api_server.py` 裡 `get_alerts` / `get_validation` 的片段，我可以依這份清單寫出對應的修改範例（因你目前在 Ask mode，我只會給程式範例，不直接改檔）。

---

**User**

Forget about @doc/model_api_protocol_v2.md , focus only on this @doc/ML_API_PROTOCOL.md . How to package the trained model?

---

**Cursor**


**`doc/ML_API_PROTOCOL.md` 裡完全沒有提到「如何 packaging 訓練好的模型」。**

這份文件只定義：

- **Base URL**：`http://localhost:8001`
- **兩個 GET API**：`/alerts`、`/validation` 的 query 參數與 response JSON
- **輪詢方式**：Dashboard 用 `ts` 做增量拉取
- **注意事項**：port 8001、無 auth、HK 時區、NaN/Inf → null

沒有描述：

- 模型檔（例如 `model.pkl`）要怎麼打包
- 模型要如何載入、放在哪裡
- 任何 scoring / inference API
- 部署或打包流程

所以在**只依賴這份文件**的前提下，**無法從中推導出「如何 package 訓練好的模型」**；文件只規範「alerts / validation 的 API 長怎樣」，不規範模型本身怎麼包。

在你們現有專案裡，「訓練好的模型」實際上是這樣被使用的：

- **訓練階段**：`trainer.py` 產出 `model.pkl`（及 `feature_list.json`、`model_version` 等）並寫入 `models/`（或 `config.MODEL_DIR`）。
- **推論階段**：`scorer.py` 用 `load_dual_artifacts()` 載入 `model.pkl`，對即時資料做 `predict_proba`，把結果寫進 SQLite 的 `alerts` 表。
- **對外介面**：`api_server.py` 只從同一個 DB 讀 `alerts` / `validation_results`，透過 HTTP 暴露給 Dashboard。

因此，若問題是「**要怎麼 package 訓練好的模型？**」：

- **若「package」指的是「給 Dashboard 用的對外介面」**：  
  依 `ML_API_PROTOCOL.md` 做的是「**把已經寫進 DB 的 alerts/validation 用 API 暴露出來**」，不需要在 API 這一層再「包」模型；模型已經被 scorer 用過了，結果在 DB 裡。
- **若「package」指的是「把 model.pkl 等產物打包成可部署的 artifact」**：  
  這不在 `ML_API_PROTOCOL.md` 的範圍內，需要另訂規範（例如：目錄結構、必要檔案、版本標示、誰在何時載入模型），或參考你們內部/其他文件。

**結論**：單看 `doc/ML_API_PROTOCOL.md`，無法得到「如何 package 訓練好的模型」的作法；這份文件只規範 alerts/validation 的 API。要實作「模型打包」，需要別的需求或文件來定義。

---

**User**

My thinking is 把 model.pkl 等產物打包成可部署的 artifact. So after training, we get a model and artifacts, then we have a script picks only the necessary items and package them (into whatever format that is standard for inference, your suggestion). Then we start a server (default host http://localhost:8001) and expose these 2 endpoints.

---

**Cursor**


你的流程可以整理成：**訓練產物 → 只打包 inference 需要的東西 → 用一個 server 在 8001 提供那兩個 GET**。下面直接對應這三塊。

---

## 1. 打包：訓練後只留「推論必要」的 artifact

訓練完後，scorer（或任何 inference 程式）真正用到的東西在 codebase 裡已經固定了，建議**打包成一個「model bundle」目錄**（必要時再壓成一個檔）。

**建議納入的檔案（從 `trainer` 的 `MODEL_DIR` 挑）：**

| 檔案 | 用途 |
|------|------|
| `model.pkl` | 主模型（v10 single rated）；沒有則 fallback 用 rated_model.pkl / walkaway_model.pkl |
| `feature_list.json` | 特徵順序與名稱，predict 時欄位要一致 |
| `feature_spec.yaml` | 若存在則用 frozen 版（train–serve 一致） |
| `model_version` | 版本字串（可選，給維運/日誌用） |
| `reason_code_map.json` | 若 scorer 有做 SHAP reason codes 就需要 |

可選：`training_metrics.json`（給維運看，不影響 inference）。

**「標準格式」建議：**

- 不一定要上 MLflow/ONNX 等重框架；你們已經是 LightGBM + joblib，**用「一個目錄 + 固定檔名」當標準**就夠。
- 目錄結構例如：
  - `bundle/
    - model.pkl
    - feature_list.json
    - feature_spec.yaml   # 若有
    - model_version
    - reason_code_map.json
  `
- 若要傳輸或版控，再對這個目錄做 **tar.gz 或 zip**（例如 `model_bundle_<version>.tar.gz`），部署時解壓到固定路徑即可。

**腳本要做的事（概念）：**

- 輸入：訓練輸出目錄（例如 `models/` 或 `out_trainer/models/`）。
- 輸出：一個「bundle 目錄」或一個 `model_bundle_<version>.tar.gz`。
- 邏輯：只複製上面列出的檔案到 bundle；若沒有 `model.pkl` 再考慮複製 `rated_model.pkl` / `walkaway_model.pkl`（與現有 scorer fallback 一致）。可順便寫一個 `MANIFEST.txt` 或 `bundle_version` 記錄版本與檔案列表，方便日後檢查。

這樣就是「把 model.pkl 等產物打包成可部署的 artifact」的具體做法，且與 ML_API_PROTOCOL 無關、可單獨做。

---

## 2. Server（預設 http://localhost:8001）與兩個 endpoint

ML_API_PROTOCOL 只定義這兩個：

- **GET /alerts**
- **GET /validation**

這個 server **不必載入 model.pkl**：  
它只負責讀 DB（或任何你們放「已算好的 alerts / validation 結果」的儲存），回傳 JSON。  
也就是說：

- **誰用「打包好的 model」？**  
  是 **scorer**（或另一個常駐/排程的 inference 程式）：載入 bundle 裡的 `model.pkl` + feature list 等，對即時資料做 `predict_proba`，把結果寫進 DB（例如 `alerts`、或再經 validator 寫 `validation_results`）。
- **誰對外提供 API？**  
  就是這個 **server**：  
  - 預設 `host=0.0.0.0` 或 `127.0.0.1`，**port=8001**  
  - 只實作 GET `/alerts` 和 GET `/validation`，照 ML_API_PROTOCOL 的 query 與 response 回傳。

所以「打包 model」和「啟動 server 暴露兩個 endpoint」是兩件事，但可以同一個 repo／同一個部署流程裡完成：

1. **打包階段**：腳本從訓練產物產出 model bundle（目錄或 tar.gz）。  
2. **部署／運行階段**：  
   - 把 bundle 解壓到某個固定路徑，給 scorer 用；  
   - scorer 寫入 DB；  
   - server 在 8001 只讀 DB，暴露 `/alerts` 與 `/validation`。

---

## 3. 流程串起來（對應你的想法）

- **After training**  
  - 得到：`model.pkl`、`feature_list.json`、`feature_spec.yaml`、`model_version`、`reason_code_map.json` 等。
- **Packaging script**  
  - 只挑上述必要（+ 可選）檔案 → 輸出一個 **model bundle**（目錄或 tar.gz），當成「可部署的 inference artifact」。
- **Start server（預設 http://localhost:8001）**  
  - 只提供 GET `/alerts`、GET `/validation`（讀 DB，不讀 model）。  
  - 若你們希望「部署時只帶一個 process」，也可以同一個 process 裡跑：  
    - 一個 thread/process 跑 scorer（載入 bundle、寫 DB），  
    - 一個 thread/process 跑 API server（8001，只讀 DB）。  
  但 API 本身仍然只暴露這兩個 endpoint，不暴露 model。

這樣就符合你說的：  
「訓練得到 model + artifacts → 用腳本打包成可部署的 inference 用 artifact → 啟動 server 在 8001 暴露這兩個 endpoint」。  
若你願意，下一步可以具體寫出「打包腳本」的輸入/輸出路徑與偽代碼（或你們現有目錄結構），我可以按你們 repo 的實際路徑幫你對齊一版。

---

**User**

Yes please draft a plan

---

**Cursor**


查看訓練輸出與 scorer 的實際路徑與依賴，以撰寫具體計畫。



以下是可直接照做或貼進 `doc/` 的計畫草稿（我只提供文字，不新增檔案）。

---

# Plan: Model Bundle Packaging & ML API Server (port 8001)

**目標**：訓練後產出可部署的 model bundle，並以預設 `http://localhost:8001` 提供 GET `/alerts` 與 GET `/validation`（依 `doc/ML_API_PROTOCOL.md`）。

---

## 1. 現況摘要

| 項目 | 現況 |
|------|------|
| 訓練產出目錄 | `trainer/models/`（`config.MODEL_DIR` = `BASE_DIR / "models"`） |
| 訓練寫入的檔案 | `model.pkl`, `feature_list.json`, `feature_spec.yaml`, `model_version`, `reason_code_map.json`, `training_metrics.json`, `walkaway_model.pkl`（legacy） |
| Scorer 讀取 | `scorer.load_dual_artifacts(model_dir)`：同上目錄，fallback 順序 model.pkl → rated_model.pkl → walkaway_model.pkl |
| API Server | `trainer/api_server.py`：port **8000**，路徑 **/get_alerts**、**/get_validation**，讀 `trainer/local_state/state.db` |
| 協定要求 | Port **8001**，路徑 **/alerts**、**/validation**，query/response 格式見 `ML_API_PROTOCOL.md` |

---

## 2. 計畫範圍

1. **Packaging script**：從訓練輸出目錄選出 inference 必要檔案，產出「可部署 model bundle」（目錄 + 可選壓縮檔）。
2. **API 對齊協定**：同一支 server 改為 port 8001，並提供 GET `/alerts`、GET `/validation`，行為與回應格式符合 `ML_API_PROTOCOL.md`（含 24h 預設、`limit`、欄位與 `casino_player_id` / `is_known_player`）。
3. **啟動方式**：預設以 `http://localhost:8001` 啟動該 server（可選：與 scorer 同機或同 process 的說明）。

不包含：改動訓練流程、改動 scorer 內部邏輯、實作 `/score` 或其他協定外 endpoint。

---

## 3. Phase A：Model bundle 打包

### 3.1 Bundle 內容（與 scorer / backtester 一致）

| 檔案 | 必要 | 說明 |
|------|------|------|
| `model.pkl` | 是（或下列 fallback 之一） | v10 主 artifact |
| `rated_model.pkl` | 若無 model.pkl 可選 | Legacy fallback |
| `walkaway_model.pkl` | 若無 model.pkl 可選 | Legacy fallback |
| `feature_list.json` | 是 | 特徵順序與名稱 |
| `feature_spec.yaml` | 建議 | Frozen spec（train–serve 一致） |
| `model_version` | 建議 | 版本字串 |
| `reason_code_map.json` | 建議 | SHAP reason codes |
| `training_metrics.json` | 可選 | 維運/稽核用 |

打包時至少要有：一個可載入的 model 檔（model.pkl 或 fallback）+ `feature_list.json`；其餘建議一併複製以利 scorer/backtester 直接使用。

### 3.2 輸出格式

- **目錄**：例如 `deploy/bundles/<version>/` 或 `dist/model_bundle_<version>/`，內含上表檔案。
- **可選壓縮**：對該目錄打 `model_bundle_<version>.tar.gz`（或 `.zip`）方便傳輸／版控。
- **MANIFEST**（可選）：在 bundle 目錄寫 `MANIFEST.txt` 或 `bundle_info.json`，列出檔案清單與 `model_version`，方便部署時檢查。

### 3.3 打包腳本規格

- **位置**：建議 `trainer/scripts/package_model_bundle.py`（或 `trainer/package_model_bundle.py`）。
- **輸入**：
  - `--source-dir`：預設 `trainer/models`（或 `config.MODEL_DIR`）。
  - `--output-dir`：預設例如 `deploy/bundles` 或 `dist`。
  - `--version`：可選，若未提供則從 `source-dir/model_version` 讀取，若無則用時間戳。
  - `--archive`：可選，若設則額外產出 `.tar.gz`。
- **邏輯**：
  1. 檢查 source 至少存在 `model.pkl` 或 `rated_model.pkl` 或 `walkaway_model.pkl` 之一，以及 `feature_list.json`。
  2. 建立 `output-dir/<version>/`（或 `output-dir/model_bundle_<version>/`）。
  3. 複製上表檔案（若存在才複製，避免因缺少可選檔而失敗）。
  4. 可選：寫入 MANIFEST / bundle_info。
  5. 可選：若 `--archive`，對該目錄建立 `model_bundle_<version>.tar.gz`。
- **錯誤**：缺少必要檔案時明確報錯並 exit non-zero。

### 3.4 使用方式（建議）

- 訓練完成後手動或由 CI 執行：
  - `python -m trainer.scripts.package_model_bundle --source-dir trainer/models --output-dir deploy/bundles --archive`
- 部署時：解壓或直接使用 bundle 目錄，將路徑設為 scorer 的 `--model-dir`（或等價設定），scorer 繼續用 `load_dual_artifacts(model_dir)` 即可。

---

## 4. Phase B：API Server 對齊 ML_API_PROTOCOL.md

### 4.1 Port 與路徑

- 預設 **port 8001**（可透過 CLI 或環境變數覆寫，例如 `ML_API_PORT`）。
- 提供：
  - **GET `/alerts`**
  - **GET `/validation`**
- 既有 `/get_alerts`、`/get_validation` 可保留並轉發到同一邏輯，或改為僅保留 `/alerts`、`/validation`，依團隊偏好（協定以 `/alerts`、`/validation` 為準）。

### 4.2 GET `/alerts` 行為與回應

- **Query**：
  - `ts`（可選）：只回傳 `ts > ts` 的 alerts。
  - `limit`（可選）：僅在**未帶 `ts`** 時生效，限制筆數。
  - **無任何參數**：回傳**過去 24 小時**的 alerts（依 `ts` 過濾）。
- **Response**：  
  - 僅包含協定欄位：`bet_id`, `ts`, `bet_ts`, `player_id`, `casino_player_id`, `table_id`, `position_idx`, `session_id`, `visit_avg_bet`, `is_known_player`。  
  - `casino_player_id`：可固定 `null`（若 DB 無此欄則在組 response 時補上）。  
  - `is_known_player`：對應 DB 的 `is_rated_obs`（1/0），若無則預設 0。  
  - NaN/Inf → `null`；timestamps 為 HK (`+08:00`)。

### 4.3 GET `/validation` 行為與回應

- **Query**：
  - `ts`：只回傳驗證時間 **after** `ts`。
  - `bet_id` / `bet_ids`：單筆或逗號分隔多筆。
  - **無任何參數**：回傳**過去 24 小時**的 validation 結果（依 `validated_at` 過濾）。
- **Response**：  
  - 欄位：`ts`, `player_id`, `casino_player_id`, `bet_id`, `walkaway_ts`, `TP`, `sync_ts`, `reason`, `bet_ts`。  
  - `casino_player_id`：可固定 `null`。  
  - `TP`：協定為字串（如 "TP"）；若 DB 存 0/1，需在 API 層轉成字串（例如 1→"TP", 0→"FP" 或 "MISS"，與 wa1 確認）。  
  - NaN/Inf → `null`；timestamps 為 HK。

### 4.4 實作要點

- 在 `api_server.py`（或共用 helper）中：
  - 無 `ts` 時，對 alerts 使用 `now_hk - 24h` 作為下限；對 validation 使用 `validated_at > now_hk - 24h`。
  - `/alerts` 無 `ts` 時若有 `limit`，先套 24h 再取前 `limit` 筆。
  - 回傳前只選協定欄位並補齊 `casino_player_id`、`is_known_player`（及 `TP` 字串化）。
- CORS `*`、Content-Type JSON、HK 時區維持現有實作即可。

### 4.5 啟動方式

- 預設：`python -m trainer.api_server` 或 `python trainer/api_server.py`，監聽 `0.0.0.0:8001`（或 `localhost:8001`）。
- Port 可從環境變數讀取，例如 `ML_API_PORT=8001`，若未設則 8001。
- 文件/README 註明：Dashboard 使用 `http://localhost:8001`，GET `/alerts`、GET `/validation`。

---

## 5. 端到端流程（建議）

1. **訓練**：執行現有 pipeline → 產出 `trainer/models/`。
2. **打包**：執行 `package_model_bundle.py` → 產出 `deploy/bundles/<version>/`（及可選 `.tar.gz`）。
3. **部署**：將 bundle 目錄放到目標機，設定 scorer 的 model 目錄指向該 bundle；scorer 寫入 `local_state/state.db`（alerts / 相關表）；validator 寫入 validation_results。
4. **啟動 API**：同一台機上執行 API server，port 8001，讀取同一 `state.db`，對外提供 GET `/alerts`、GET `/validation`。
5. **Dashboard**：輪詢 `http://localhost:8001/alerts`、`http://localhost:8001/validation`（依 `ML_API_PROTOCOL.md` 的 polling 建議）。

---

## 6. 檔案／變更清單（實作時對照）

| 項目 | 類型 | 說明 |
|------|------|------|
| `trainer/scripts/package_model_bundle.py` | 新增 | 打包腳本（輸入 source dir、output dir、可選 version、可選 archive）。 |
| `trainer/api_server.py` | 修改 | Port 改 8001；新增或改為 GET `/alerts`、GET `/validation`；實作 24h 預設、`limit`、協定欄位與 `casino_player_id`/`is_known_player`/`TP`。 |
| `doc/ML_API_PROTOCOL.md` | 不變 | 僅作行為與格式依據。 |
| 可選：`deploy/README.md` 或 `doc/DEPLOY.md` | 新增 | 簡短說明：如何執行打包、部署目錄結構、如何啟動 server 與 scorer。 |

---

## 7. 風險與注意

- **DB 與時區**：alerts/validation 的「過去 24 小時」建議以 HK 的「現在」為基準計算，避免 server 與 DB 時區不一致。
- **向後相容**：若現有前端仍呼叫 `/get_alerts`、`/get_validation`，可保留兩組 route 並共用同一邏輯，避免破壞現有整合。
- **記憶體**：API 只做 DB 查詢與序列化，不載入 model；若單次回傳筆數過大，可依協定用 `ts` + `limit` 做分頁，避免一次拉全表。

若要，我可以把「Phase A 打包腳本」或「Phase B API 修改」拆成逐步實作清單（step-by-step checklist）方便你直接對應到檔案與函式名稱。

---

**User**

Please save this plan to a file under @package , then implement this plan within this folder.

---

**Cursor**


正在把計畫存到 `package/` 並在該資料夾內實作：建立 `package/`、寫入計畫檔，並實作打包腳本與 API 對齊。



Implementing protocol-compliant GET `/alerts` and GET `/validation` in `api_server.py`, and setting default port to 8001.



Updating get_alerts to use shared query logic so both paths read from the same source, and setting the default port to 8001.



Preserving legacy `/get_alerts` behavior: when `ts` is absent, return all alerts (no 24h default). Applying 24h default only for `/alerts`.



Adding a `default_24h` option to the validation query and refactoring `get_validation` to use it while preserving legacy behavior.



Refactoring `get_validation` to use `_query_validation_df` while preserving its legacy response shape:



Making protocol alert response robust to missing DB columns:



Verifying the packaging script runs (--help and missing-source handling):



Summary of what’s in place:

---

## 1. Plan and docs under `package/`

- **`package/PLAN.md`** – Plan for model bundle packaging and ML API (port 8001, GET `/alerts`, GET `/validation`).
- **`package/README.md`** – How to run the packager and the API server.

---

## 2. Packaging script in `package/`

- **`package/__init__.py`** – Makes `package` a Python package.
- **`package/package_model_bundle.py`** – Script that:
  - Takes `--source-dir` (default `trainer/models`), `--output-dir` (default `package/bundles`), optional `--version`, optional `--archive`.
  - Requires at least one of `model.pkl`, `rated_model.pkl`, `walkaway_model.pkl` and `feature_list.json`.
  - Copies all present bundle files (e.g. `feature_spec.yaml`, `model_version`, `reason_code_map.json`, `training_metrics.json`) into `output_dir/<version>/`.
  - Writes `bundle_info.json` with version and file list.
  - With `--archive`, builds `model_bundle_<version>.tar.gz`.
  - Exits with code 1 and a clear message if required files are missing.

**Usage from repo root:**

```bash
python -m package.package_model_bundle --source-dir trainer/models --output-dir package/bundles --archive
```

---

## 3. API server changes in `trainer/api_server.py`

- **Default port 8001** – Uses `ML_API_PORT` if set, otherwise 8001.
- **GET `/alerts`** (protocol):
  - Query: `ts` (only alerts after this time), `limit` (used only when `ts` is absent).
  - No query params → last 24 hours.
  - Response: only protocol fields; `casino_player_id` = `null`, `is_known_player` from `is_rated_obs`.
- **GET `/validation`** (protocol):
  - Query: `ts`, `bet_id`, `bet_ids`.
  - No params → last 24 hours.
  - Response: protocol fields; `casino_player_id` = `null`, `TP` as string (`"TP"`/`"FP"`).
- **Legacy** – `/get_alerts` and `/get_validation` unchanged (no 24h default, same response shape).
- Shared helpers: `_query_alerts_df`, `_query_validation_df`, `_alerts_to_protocol_records`, `_validation_to_protocol_records`.

**Start server (from repo root):**

```bash
python -m trainer.api_server
```

Then: `http://localhost:8001/alerts`, `http://localhost:8001/validation`.

---

## 4. Quick checks

- `python -m package.package_model_bundle --help` runs and shows options.
- With a missing/invalid source, the script exits with code 1 and the expected error message.
- Lint for `api_server.py` and `package_model_bundle.py`: no issues reported.

---

**User**

Also add a readme doc in @package/ to show user how to use this

---

**Cursor**


Expanding the existing README to add a clearer "What is this" section, step-by-step usage, example API calls, and brief troubleshooting notes.



Updated **`package/README.md`** so it works as a clear user-facing guide. It now includes:

- **What this folder is for** — bundle packaging and ML API server.
- **How to use**
  - **1. Package the model** — Commands with defaults, and a table of all options (`--source-dir`, `--output-dir`, `--version`, `--archive`).
  - **2. Run the ML API server** — How to start it, port override, and a table of the two endpoints.
  - **3. End-to-end flow** — Train → Package → Deploy → Serve.
- **Example API calls** — `curl` examples for `/alerts` and `/validation`.
- **Files in this folder** — Short description of README, PLAN, script, and `bundles/`.
- **Troubleshooting** — Common issues (no model artifact, empty responses, port in use).

---

**User**

wait, what if I need to move this bundle to another machine, and that machine may not have Python?

---

**Cursor**


目前這個 **bundle 只包含「檔案」**（`model.pkl`、`feature_list.json` 等），**不包含執行環境**。  
要「載入 model.pkl 並做推論」，一定要有 **Python + 依賴**（joblib、lightgbm、pandas 等），所以：

- **若目標機「完全沒有 Python」**：  
  不能在那台機器上直接跑現在的 scorer / 任何用 Python 載入 bundle 的程式。  
- **若目標機「只是要顯示或接收 alerts」**：  
  那台機器不必跑模型，只要能呼叫 API 或讀取已算好的資料即可（API 可以跑在另一台有 Python 的機器上）。

你可以依需求選下面幾種做法：

---

### 1. 不在那台機器跑推論（最單純）

- **有 Python 的機器 A**：放 bundle、跑 scorer、跑 API server（port 8001）。  
- **沒 Python 的機器 B**：只跑 Dashboard，用 `http://<A>:8001/alerts` 和 `/validation` 拉資料。  
- Bundle 只部署在 A，B 不需要 bundle、也不需要 Python。

---

### 2. 把「Python + 依賴」一起搬到目標機（自包含部署）

若你一定要在「另一台機器」上跑推論，就必須讓那台機器**有 Python 環境**，但可以打包成一個自包含的單位，例如：

- **Docker**：映像裡裝好 Python、依賴、scorer 程式與 bundle，部署時 `docker run ...`，那台機器只需要 Docker，不必自己裝 Python。
- **Conda-pack / venv + 壓縮**：在開發機用 conda 或 venv 裝好依賴，打包成 tar；在目標機解壓後用該環境的 `python` 跑 scorer。
- **PyInstaller / 類似工具**：把 scorer 打成單一執行檔（仍會內嵌 Python runtime），搭配 bundle 目錄一起複製過去。  
  （LightGBM + joblib 打進單一執行檔較容易遇到路徑或動態載入問題，需要多試一下。）

這樣「搬過去的」是：**bundle + 執行環境（或執行檔）**，目標機不必事先安裝 Python。

---

### 3. 不用 Python 跑推論（換格式 / 換語言）

若目標機**堅持不裝 Python、也不跑 Docker**，就要脫離「Python + joblib」：

- **LightGBM 原生格式**：用 `booster.save_model()` 存成文字或二進位，用 **LightGBM 的 C/C++/R/其他語言 API** 在那台機器上載入並推論；bundle 裡就改成放這種檔案，而不是 `model.pkl`。  
  需要在那邊寫/編譯一小段推論程式（非 Python）。
- **ONNX**：把模型轉成 ONNX，用 **ONNX Runtime**（C++/C#/Java 等）在那台機器推論。  
  需要轉換流程與對應的推論程式。

目前 repo 的 scorer 是 Python + joblib，若要走這條，就要**另做**轉換與推論程式，bundle 內容和用法都會和現在不一樣。

---

### 總結

- **Bundle 本身**：只是檔案集合，**不包含** Python；移到另一台機器只是「複製檔案」。
- **在那台機器「用」這個 bundle 做推論**：  
  - 要嘛那台機器要有 Python（或你帶過去：Docker / conda-pack / 執行檔），  
  - 要嘛改成用非 Python 的格式與程式（LightGBM 原生、ONNX 等）。

若你告訴我目標機的條件（可不可以裝 Docker / 單一執行檔 / 只准 C++ 等），可以再具體說「該選 2 還是 3、以及 bundle 要怎麼包、要放哪些東西」。

---

**User**

不可以裝 Docker, and prefer 單一執行檔

---

**Cursor**


在不裝 Docker、又希望是**單一執行檔**的前提下，可行做法是：用 **PyInstaller**（或 **Nuitka**）把 Python 直譯器、scorer／API 程式與依賴打成**一個（或兩個）執行檔**，部署時搭配**外部的 model bundle 目錄**一起帶過去，目標機不需要安裝 Python。

---

## 做法概覽

- **單一執行檔**：用 PyInstaller / Nuitka 把「推論 + 對外 API」打成一個 `.exe`（Windows）或無副檔名執行檔（Linux）。
- **Model bundle 不包進 exe**：`model.pkl`、`feature_list.json` 等仍放在**執行檔旁邊的目錄**（例如 `./models/` 或 `./bundle/`），由程式在執行時讀取。這樣換模型時只要換目錄，不必重打包。
- **目標機**：只放「執行檔 + bundle 目錄 + 必要設定」，不裝 Python、不裝 Docker。

---

## 實作要點

### 1. 要打進執行檔的內容

- **程式**：scorer 的入口（例如 `trainer/scorer.py` 的 `main`）＋提供 GET `/alerts`、GET `/validation` 的 API（可抽成一個精簡 Flask app，只做讀 DB、回傳 JSON）。
- **套件**：`trainer` 底下被用到的模組（config、features、identity、schema_io、db_conn、scorer 等）、以及：
  - `flask`、`pandas`、`numpy`、`joblib`、`lightgbm`
  - 若有用到 `zoneinfo`/`backports.zoneinfo`、`dotenv` 等也要一併包進去。
- **不包進 exe**：model bundle 目錄（讓使用者可替換）。

### 2. 執行檔如何找 bundle 與 DB

- 用**相對於執行檔的路徑**找 bundle，例如：
  - 執行檔同目錄下的 `models/` 或 `bundle/`；
  - 或由**環境變數**／同目錄的設定檔指定（例如 `MODEL_DIR=./models`）。
- SQLite DB（`state.db`）可寫在執行檔同目錄的 `local_state/`，或由環境變數指定。這樣 scorer 寫入、API 讀取同一份 DB。

### 3. PyInstaller 注意事項

- **LightGBM**：會帶 `.so`/`.dll`，PyInstaller 多半能包進去，但若漏掉要用 `--hidden-import=lightgbm` 或寫 `hook-lightgbm.py`。
- **體積**：pandas + numpy + lightgbm 會讓 exe 很大（幾百 MB 是常態），屬正常。
- **建議**：在「乾淨的 Windows 虛擬機」或目標環境同 OS 上建一次、跑一次，確認無缺少 DLL／路徑問題。

### 4. 一個 exe 還是兩個

- **一個 exe**：內含「scorer 迴圈 + 簡易 API server」同一個 process（例如一個 thread 跑 scorer、一個跑 Flask）。部署最簡單：一個執行檔 + 一個 bundle 目錄。
- **兩個 exe**：一個只跑 scorer（寫 DB）、一個只跑 API（讀 DB）。好處是更新模型或 API 可分別重打包，壞處是部署要跑兩個程式、要協調誰先起。

若你偏好單一執行檔，就做**一個 exe**，裡面同時跑 scorer 與 API。

### 5. 建置流程（概念）

- 在**有 Python 的開發機**上：
  1. 建立 venv，安裝所有依賴（trainer 用到的 + flask、joblib、lightgbm 等）。
  2. 用 PyInstaller 指定 scorer+API 的入口腳本，加上 `--hidden-import`、必要時 `--add-data`（若有無法自動偵測的檔案）。
  3. 產出在 `dist/` 底下的單一執行檔。
- 部署到目標機：
  - 複製該執行檔 + 用 `package_model_bundle.py` 產出的 bundle 目錄（例如 `package/bundles/<version>/`）到同一層或指定路徑；
  - 執行檔啟動時從該目錄讀 model、寫/讀 `state.db`，並 listen port 8001 提供 `/alerts`、`/validation`。

---

## 小結

- **可以**在不裝 Docker、目標機沒有 Python 的情況下，用 **PyInstaller（或 Nuitka）做成單一執行檔**，搭配**外部的 model bundle 目錄**一起部署。
- **Model bundle 維持現在這種「一包檔案」就好**，不必包進 exe，方便日後換模型只換目錄。
- 若你願意，之後可以在 Agent 模式裡具體做：一個「可被 PyInstaller 呼叫的入口腳本」＋一個 `package/spec` 或建置說明（要包哪些模組、參數怎麼設），讓你們團隊能重現建置。

---

**User**

What I want is to be able to move the whole thing around, with a Python/Flask script to set it up on a target machine. Note that we cannot assume we can install anything (e.g. Docker, Python package etc.) on it only except for packages necessary for setting up the host. This host then exposes the mentioned 2 endpoints to external.

---

**Cursor**


你的需求可以整理成：

- **可整包搬移**：一個「完整單元」能複製到目標機。
- **用 Python/Flask 腳本做設定**：在目標機上只靠一個（或多個）Python/Flask 相關腳本完成「設定」。
- **目標機不能隨便裝東西**：只能安裝「設定這台 host 所需」的套件（例如用來跑這個服務的 Python 與依賴）；不假設有 Docker 或其它多餘軟體。
- **這台 host 對外只暴露那兩個 API**：GET `/alerts`、GET `/validation`。

下面分兩種情境說明「整包內容」與「設定腳本」該做什麼。

---

## 兩種 host 角色

### A. Host 只做「API server」（不跑推論）

- 這台機器**只**讀 SQLite（`state.db`），用 Flask 提供 GET `/alerts`、GET `/validation`。
- **不需要** model bundle、LightGBM、scorer；alerts/validation 已在別處算好，再把 DB 複製過來，或由別台寫入。

**要搬的「整包」：**

- 一個資料夾（或壓縮檔），例如 `ml_api_deploy/`，內含：
  - **設定／啟動用腳本**（Python）：例如 `setup.py` 或 `run_server.py`。
  - **Flask 應用**：一隻（或少量）腳本，只實作這兩個 endpoint，讀同目錄下的 `local_state/state.db`（或設定檔指定的路徑）。
  - **`requirements.txt`**：只列 API 所需（例如 `flask`，若用 pandas 做日期再列 `pandas`；讀 SQLite 可用標準庫，不一定要 pandas）。
  - （可選）**預先填好的 `local_state/state.db`**：若你們是「在別處跑 scorer，再把 DB 複製過來」。
  - （可選）**`wheels/`**：把依賴打成 wheel，供離線 `pip install --no-index -f wheels -r requirements.txt`，這樣目標機不必連 PyPI。

**設定腳本該做的事：**

1. 檢查 Python 版本（例如 ≥3.9）。
2. （建議）在專案目錄下建 venv：`python -m venv .venv`，然後用 `.venv/bin/pip` 或 `.venv\Scripts\pip` 裝套件。
3. 安裝依賴：  
   - 有網路：`pip install -r requirements.txt`  
   - 僅能離線：`pip install --no-index -f wheels -r requirements.txt`
4. 若沒有 `local_state/state.db`，可建立空 DB 並建表（與現有 `api_server` 的 schema 一致），或說明「需從別處複製 state.db」。
5. 啟動服務：例如 `python run_server.py` 或 `flask run`，listen 在 8001、對外提供 `/alerts` 與 `/validation`。  
   - 可寫成「設定腳本最後一步就是啟動」，或「設定腳本只負責裝好環境並印出執行指令」。

這樣就符合：**整包可搬移、用 Python/Flask 腳本設定、只裝「跑這個 API 所需」的套件、host 對外只暴露這兩個 endpoint**。

---

### B. Host 同時跑「scorer + API」（會在這台做推論）

- 這台機器要：載入 model bundle、跑 scorer 寫入 `state.db`，並跑 Flask 提供 `/alerts`、`/validation`。
- 需要：Python、Flask、pandas、numpy、joblib、lightgbm、以及你們的 scorer/feature 等程式。

**要搬的「整包」：**

- 同上，但**多加**：
  - **model bundle 目錄**（`model.pkl`、`feature_list.json` 等，可用現有 `package_model_bundle.py` 產出）。
  - **scorer 相關程式**（或精簡成「只做推論 + 寫 alerts 的那一塊」）。
  - **`requirements.txt`** 要包含：flask、pandas、numpy、joblib、lightgbm，以及其它 scorer 依賴（例如 pyyaml、clickhouse-driver 等，依你們實際 import）。
  - （可選）**wheels/** 做離線安裝。

**設定腳本該做的事：**

- 與 A 相同：檢查 Python、建 venv、`pip install -r requirements.txt`（或從 wheels）。
- 確認 **MODEL_DIR**（或等價設定）指向整包裡的 bundle 目錄；**STATE_DB_PATH** 指向本機的 `local_state/state.db`。
- 啟動方式二選一：  
  - **單一 process**：一個 Python 程式內同時跑「scorer 迴圈」和「Flask app」（例如一個 thread 跑 scorer、一個跑 Flask），設定腳本只啟動這一個程式。  
  - **兩個 process**：設定腳本先啟動 scorer（寫 DB），再啟動 Flask（讀 DB）；或提供兩條指令讓維運分別啟動。

同樣符合：整包可搬、用 Python 腳本設定、只裝必要套件、host 對外只暴露這兩個 endpoint（推論在 host 內部完成）。

---

## 建議的「整包」目錄結構（概念）

```text
ml_api_deploy/
├── setup.py              # 或 run_server.py：檢查環境、pip install、必要時建 DB、啟動服務
├── requirements.txt      # 僅 API：flask[, pandas]；含 scorer：+ lightgbm, joblib, ...
├── app/
│   └── server.py         # Flask app：GET /alerts, GET /validation，讀 state.db
├── local_state/          # 可選：放 state.db，或由 setup 建立空 DB
├── models/               # 僅 B：從 package_model_bundle 產出的 bundle 目錄
│   ├── model.pkl
│   ├── feature_list.json
│   └── ...
└── wheels/               # 可選：離線用
    └── ...
```

- **設定腳本**（例如 `setup.py`）：  
  - 用標準庫或最小依賴（例如只用到 `subprocess`、`venv`）做 venv、pip install、路徑設定；若希望「設定腳本本身」也少依賴，可以讓它只做 venv + pip install，再 `subprocess.run([venv_python, "app/server.py"])` 啟動一個「合併 scorer+API」的 `server.py`。

---

## 重點整理

| 項目 | 說明 |
|------|------|
| **「整包」** | 一個資料夾（或 tar/zip），內含：設定腳本、Flask app、requirements.txt、必要時 model bundle 與 state.db（或空 DB）、可選 wheels。 |
| **「只裝必要套件」** | 透過 `requirements.txt`（或 wheels）只裝跑這個服務需要的套件（Flask、pandas、lightgbm 等），不裝 Docker 或其它。 |
| **Python** | 假設目標機可安裝或已具備「用來設定 host 的 Python」；設定腳本用這個 Python 建 venv 並裝依賴。 |
| **對外** | Host 只暴露 GET `/alerts`、GET `/validation`（依 `doc/ML_API_PROTOCOL.md`）；實作可沿用你們現有 `api_server` 的邏輯，抽成一個可被「整包」帶過去的 Flask app。 |

若你願意，下一步可以具體寫出：**一個最小的 `requirements.txt`**（API-only 與 scorer+API 各一版）和 **`setup.py` / `run_server.py` 的偽碼或介面**，方便你對應到現有 repo 的 `trainer/api_server.py` 與 scorer。

---

**User**

We go for B. Host 同時跑「scorer + API」（會在這台做推論）

---

**Cursor**


採用 **B：Host 同時跑 scorer + API** 時，可依下面方式設計「整包」與設定流程，之後在 Agent 模式再實作即可。

---

## 1. 整包目錄結構（建議）

建議放在 **`package/deploy/`**（或單獨的 `deploy/`），方便從現有 repo 建包：

```text
package/deploy/
├── README.md                 # 部署說明：如何複製整包、如何執行 setup
├── setup.py                  # 唯一入口：檢查 Python、建 venv、pip install、啟動服務
├── requirements.txt          # Flask, pandas, numpy, joblib, lightgbm, pyyaml, ...（與 scorer + API 一致）
├── app/
│   └── server.py             # 單一 process：scorer 迴圈（thread） + Flask（/alerts, /validation）
├── local_state/              # 空目錄或 .gitkeep；執行時會產生 state.db
├── models/                  # 部署時由「package_model_bundle 產出」複製進來，或符號連結
└── wheels/                  # 可選：離線用 pip install --no-index -f wheels -r requirements.txt
```

- **整包** = 整個 `package/deploy/`（或建包腳本產生的 `ml_api_deploy_<version>.tar.gz`）可整份複製到目標機。
- **`models/`**：用現有 `package_model_bundle.py` 產出的 bundle 目錄內容放進來（或建包腳本從 `package/bundles/<version>/` 複製）。

---

## 2. 單一 process：scorer + API 一起跑

- **一個 Python process**，裡面：
  - **主 thread**：Flask app，listen 8001，只提供 GET `/alerts`、GET `/validation`（邏輯與現有 `trainer/api_server.py` 的 protocol 一致，讀 `local_state/state.db`）。
  - **背景 thread**：跑 scorer 迴圈（與現有 `trainer/scorer.py` 的 `score_once` + 輪詢邏輯相同）：載入 `models/` 的 bundle、拉資料、推論、寫入同一個 `local_state/state.db`。
- **`setup.py`** 只做環境準備，最後一步是執行這個單一 `app/server.py`（或等價入口），不需同時管理兩個 process。

這樣「整包 + 一個設定腳本」就能在目標機上同時跑 scorer 與 API。

---

## 3. `setup.py` 該做的事（步驟）

1. **檢查 Python**：`sys.version_info >= (3, 9)`（或你們 scorer 實際需求）。
2. **建 venv**：在 `package/deploy/` 底下 `python -m venv .venv`（若已存在可跳過）。
3. **安裝依賴**：  
   - `.venv/bin/pip install -r requirements.txt`  
   - 若有 `wheels/`：`pip install --no-index -f wheels -r requirements.txt`。
4. **檢查 `models/`**：至少存在 `model.pkl` 或 `rated_model.pkl` 或 `walkaway_model.pkl` 之一，以及 `feature_list.json`；缺則印出錯誤並說明「請先執行 package_model_bundle 並將 bundle 複製到 deploy/models/」。
5. **建立 `local_state`**：若不存在則 `mkdir local_state`。
6. **啟動服務**：  
   - 使用 venv 的 Python：`.venv/bin/python app/server.py`（或 `python -m app.server`），  
   - 或印出指令讓使用者手動執行。

不假設目標機有 Docker 或全域 pip；所有依賴都裝在 `.venv` 裡。

---

## 4. `app/server.py` 該做的事

- **Flask 部分**（與現有 API 對齊）：
  - 讀取 DB 路徑：例如 `BASE_DIR / "local_state" / "state.db"`（`BASE_DIR` = deploy 根目錄）。
  - GET `/alerts`：query `ts`、`limit`，無參數則過去 24h；回傳格式依 `doc/ML_API_PROTOCOL.md`（僅協定欄位、`casino_player_id` null、`is_known_player` 等）。
  - GET `/validation`：query `ts`、`bet_id`、`bet_ids`，無參數則過去 24h；同上協定。
  - CORS `*`，listen `0.0.0.0:8001`（或從 env `ML_API_PORT` 讀）。
- **Scorer 部分**（與現有 scorer 對齊）：
  - 在**背景 thread** 啟動，例如：  
    - `model_dir = BASE_DIR / "models"`，用與 `trainer/scorer.load_dual_artifacts(model_dir)` 相同的邏輯載入模型（或直接 import `trainer.scorer` 並呼叫其迴圈，若 deploy 包裡有 trainer）。
  - 使用同一 `state.db` 路徑做 init_db、append_alerts、prune 等；輪詢間隔與 lookback 可從 config 或 env 讀（例如 `SCORER_POLL_INTERVAL_SECONDS`、`SCORER_LOOKBACK_HOURS`）。
- **啟動順序**：先啟動 scorer thread，再啟動 Flask（或反過來亦可，Flask 讀 DB 時 scorer 可能尚未寫入，回傳空 list 即可）。

DB schema（alerts、validation_results、processed_alerts 等）需與現有 `trainer/scorer` 和 `trainer/api_server` 一致，以便同一份 `state.db` 兩邊共用。

---

## 5. 與現有 repo 的關係

- **選項 A（推薦）：deploy 包依賴 trainer 套件**  
  - `requirements.txt` 不直接列 lightgbm 等，而是用 **`-e ../..` 或 `-e /path/to/Patron_Walkaway`** 在 setup 時安裝整個專案（editable），則 `app/server.py` 可 `import trainer.scorer`、`import trainer.api_server`（或從 api_server 抽出僅含 `/alerts`、`/validation` 的 app），scorer 與 API 邏輯完全不重寫。  
  - 整包要搬移時，可改為「把 trainer 打成 wheel」：`pip wheel .` 在 repo 根目錄，把產出的 `.whl` 放進 `deploy/wheels/`，並在 `requirements.txt` 裡寫該 wheel；部署時 `pip install --no-index -f wheels <wheel>` 再 `pip install -r requirements.txt`（其餘僅 flask 等少數依賴）。
- **選項 B：複製最小程式碼到 deploy**  
  - 把 scorer 與 API 所需的最小程式碼（載入 model、推論、寫 DB、Flask 兩支 endpoint）複製到 `package/deploy/app/`，不 import trainer。好處是 deploy 完全自包含、不依賴 repo 路徑；缺點是要維護兩份邏輯或從 trainer 同步精簡版。

若希望「整包可任意搬移、目標機不裝 repo」，選 B；若可接受「建包時從 repo 打出 wheel」，選 A 較省事且與現有程式一致。

---

## 6. `requirements.txt` 範圍（B：scorer + API）

需涵蓋：

- **API**：`flask`。
- **Scorer / 推論**：`pandas`、`numpy`、`joblib`、`lightgbm`。
- **Scorer 依賴**（依你們實際 import）：例如 `pyyaml`、`python-dotenv`；若從 ClickHouse 拉資料則 `clickhouse-driver`；若有 `zoneinfo` 則 Python 3.9+ 內建。
- **與 trainer 一致**：可從 repo 的 `requirements.txt` 或 `setup.py` 擷取 scorer 真正用到的套件，避免漏掉。

若採用「安裝整個專案 wheel」，則 `requirements.txt` 可只列專案 wheel + flask（其餘由專案 wheel 依賴帶入）。

---

## 7. 建包流程（從 repo 產出可搬移的整包）

1. 訓練完成後執行：  
   `python -m package.package_model_bundle --source-dir trainer/models --output-dir package/bundles --archive`  
   得到 `package/bundles/<version>/`。
2. 建立或清空 `package/deploy/models/`，把 `package/bundles/<version>/*` 複製進去。
3. （可選）離線用：在 repo 根目錄 `pip wheel .` 與 `pip wheel -r requirements.txt`，把產出的 wheel 放進 `package/deploy/wheels/`。
4. 打包：`tar czvf ml_api_deploy_<version>.tar.gz -C package deploy`（或 zip），即為可搬移到目標機的「整包」。

目標機上：

- 解壓後進入 `deploy/`，執行：  
  `python setup.py`  
  或：  
  `python setup.py && .venv/bin/python app/server.py`  
  （依你設計的 setup 是否在最後直接啟動 server）。

---

## 8. 小結（B 方案）

| 項目 | 內容 |
|------|------|
| **整包** | `package/deploy/`（含 setup.py、app/server.py、requirements.txt、local_state、models、可選 wheels）。 |
| **設定** | 僅用 Python 腳本：venv + pip install + 檢查 models/ + 啟動 server。 |
| **Host 角色** | 同一 process 內：scorer thread（推論、寫 state.db）+ Flask（讀 state.db，GET /alerts、GET /validation）。 |
| **對外** | 只暴露這兩個 endpoint；不假設 Docker；只安裝 requirements.txt 所列套件（或透過專案 wheel）。 |

若要實作，可切換到 Agent 模式，指定：  
- 採用「依賴 trainer wheel」還是「複製最小程式到 deploy」；  
- `setup.py`、`app/server.py`、`requirements.txt` 要放在 `package/deploy/` 還是別的目錄，  
我就可以依上述規格產出對應檔案與建包步驟。

