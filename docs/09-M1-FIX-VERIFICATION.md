# M1 驗收缺陷修復與複驗

日期：2026-10-23（Asia/Taipei）。範圍僅 M1；仍全部使用 Fake Adapters。

**F-01、F-02、F-03 三個驗收缺陷已修復並通過複驗。** Windows 與 Linux 各有
116 個正式測試通過，均為 0 failed／0 skipped。前次未修改的 6 個驗收案例亦全部通過。
本次沒有 Git Push、正式環境變更、真實 Codex Runtime 或 M2 實作。

## 修復內容

| 發現 | 修復與證據 |
| --- | --- |
| F-01 配置失敗卻寫 sandbox.ready | 配置前以 CAS 記錄 sandbox.requested，保留版本鎖；配置成功後才轉 SANDBOX_READY。失敗走 QUEUED→FAILED，不寫 ready，並釋放預算 |
| F-02 併發冪等衝突漏稽核 | 將原交易與 IntegrityError replay recovery 納入同一外層 DomainError handler；序列與競爭衝突都只寫一筆拒絕事件，成功重播不變 |
| F-03 非 ASCII bearer 觸發 500 | incoming token 在 constant-time comparison 前檢查 ASCII，無效時回 401／Bearer challenge；非 ASCII server token 設定回 503，不回傳 token 值 |
| API 啟動就緒風險 | Compose 加入本機 `/health` healthcheck，`up --wait` 等 HTTP 可回應；本次容器實際為 healthy |

配置前的 CAS 不可直接移到 adapter 呼叫之後，否則可能重複配置。因此新增兩個競爭
execute 的 regression test，確認只有一個請求配置 sandbox，另一個回 409。
額外的 `sandbox.requested` 事件會多增一次版本；一般 review version 從 9 變成 10。
API 文件已更新，客戶端應一律使用回應中的 version。

## 真實測試結果

| 檢查 | 實際結果 |
| --- | --- |
| Windows，原始 `D:\Code\作品集\hogwarts-living-agent` | **116 passed, 1 warning in 29.16s** |
| Linux，Docker 一次性測試容器 | **116 passed, 1 warning in 24.92s** |
| 前次驗收原始 6 個失敗案例，未修改 assertion | **6 passed, 1 warning in 1.66s** |
| Ruff lint | All checks passed! |
| Ruff format | 17 files already formatted |
| Compose config／image build／up --wait | 全部 exit 0 |
| API HTTP health／Docker health status | status=ok；Health=healthy |
| API process user | uid=1000(appuser) gid=1000(appuser) |
| Alembic current／check | 0002 (head)；No new upgrade operations detected. |
| Compose down --volumes --remove-orphans | exit 0；本次容器、網路與資料卷已清除 |

116 個正式案例 = 原有 102 個 + 新增 14 個：新增 7 個情境各跑 SQLite 與 PostgreSQL。
涵蓋配置失敗、serial／racing 拒絕事件各一筆、非法 incoming token、兩種非法 server
token 設定，以及競爭執行只配置一次。原有授權、人工核准、狀態機、預算、冪等、
atomic claim、migration roundtrip 與 fake workflow 測試均保留且通過。

唯一 warning 是既有 Starlette TestClient `httpx` 相容介面的 deprecation，沒有隱藏。
沒有把失敗測試改成 skip／xfail。原始 6 個案例在獨立位置執行，以原專案的
`pyproject.toml` 設定載入修正後程式，不是修改舊 assertion 來取得通過。

## 修改檔案

- `apps/api/hogwarts/service.py`：事件準確性、版本鎖與一致拒絕稽核。
- `apps/api/hogwarts/auth.py`：ASCII 設定／輸入驗證。
- `tests/test_m1_regressions.py`：新增正式 regression suite。
- `compose.yaml`：API healthcheck。
- `README.md`、`docs/06-DEVELOPMENT.md`、`docs/07-API.md`：設定與版本行為說明。
- `docs/adr/0003-m1-acceptance-fixes.md`：設計理由。
- 本報告 `docs/09-M1-FIX-VERIFICATION.md`。

不需要新 migration：資料模型與 schema 未變。CI 的原有 `pytest -v` 會自動收集
新增測試，不需要改 workflow。前次驗收報告保持歷史紀錄，本報告記錄修正後結果。

## 執行與重現

本次實際採用獨立 Compose project `hogwarts-m1-fix-e7ecb111`，API port 18083，
測試 PostgreSQL port 55434；使用原 Compose 加上只調整驗收埠的 override。
測試憑證臨時產生，沒有寫入原始碼或報告。Windows 與 Linux 使用同一專用 test DB，
fixture 為每個案例建立獨立 schema，依序執行。

在原始專案根目錄，使用 Python 3.12 與專用 PostgreSQL test database 可重現：

```powershell
python -m pip install -r requirements.lock
$env:TEST_DATABASE_URL = 'postgresql+psycopg://TEST_USER:TEST_PASSWORD@localhost/DEDICATED_TEST_DB'
python -m ruff check . --no-cache
python -m ruff format --check . --no-cache
python -m pytest -v --junitxml=test-results/junit.xml
```

本次 Windows 使用 bundled Python 3.12.14 與既有工作區的依賴，沒有修改系統 Python。
完整 PowerShell 複驗腳本附於交付包，包含 build、migration、Windows suite、原始驗收
probes、Linux suite、JUnit 複製與清理。每一步 exit code 保存在 verification-results.json。

Linux Pytest 使用一次性 root 測試容器以允許寫入 `/app` 下的測試暫存檔；API service
本身仍是非 root。這不是 M2 真正 sandbox 隔離的安全性驗證。

## 判定與剩餘限制

三個缺陷已關閉；M1 本機與 Docker 複驗通過。遠端 GitHub Actions **本次未執行、仍未驗證**，
不能宣稱遠端 CI 通過。沒有因測試通過而自動 Git Push 或開始 M2。

其他已知邊界不變：本機單 owner bearer auth、in-flight reservation cap（非每日真實
付費帳務）、fake engineering evidence、尚無真實 sandbox／Codex／lease reconciler。
API healthcheck 只確認 HTTP 與資料庫連線，不宣稱完整 production readiness。

後續若要取得遠端 CI 證據，需先形成可追溯提交並取得使用者的 Push 授權。
本次交付包含完整原始碼、修復 diff、SHA-256 manifest、Windows／Linux JUnit 與原始輸出。
