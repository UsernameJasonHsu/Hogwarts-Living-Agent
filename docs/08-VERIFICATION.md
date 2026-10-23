# M0 / M1 實作與真實驗證報告

日期：2026-10-23（Asia/Taipei）。本次範圍到 M1 為止，未進入 M2–M5。

## 結果

程式已寫回 `D:\Code\作品集\hogwarts-living-agent`。最後一輪檢查與測試
直接在這個原始專案目錄執行，不只測試工作區副本。

| 項目 | 實際結果 |
| --- | --- |
| Python | 3.12.14 |
| PostgreSQL | 16.15，EDB Windows binary，127.0.0.1:55432 |
| Ruff lint | All checks passed! |
| Ruff formatting | 16 files already formatted |
| Pytest 最終結果 | **102 passed, 1 warning in 28.74s** |
| 失敗／跳過 | **0 failed / 0 skipped** |
| Migration | SQLite、PostgreSQL：upgrade / downgrade / upgrade、alembic check 全通過 |
| Docker Compose | `docker compose config --quiet` exit 0；只驗證設定 |
| Docker build / 容器啟動 | 未通過環境前提，未宣稱執行成功 |
| GitHub Actions | 已提供 CI workflow；本次未觸發或宣稱遠端 CI 成功 |

測試分布：2 個 SQLite 基礎案例，另有 50 個案例各跑 SQLite 與 PostgreSQL，
總計 102 個。每個 PostgreSQL 測試建立獨立 schema，結束後清除該 schema。
真實資料庫 migration 包含 0001 foundation 與 0002 governance tables。

唯一 warning 來自所安裝 Starlette 的 TestClient：`httpx` 相容介面已 deprecated，
建議未來遷移 `httpx2`；本次未隱藏 warning，所有測試仍實際通過。

## 架構與完成項目

M0：monorepo skeleton、FastAPI health、PostgreSQL、SQLAlchemy 2、Alembic、
Pydantic 2、Pytest、Ruff、Dockerfile／Compose、CI、環境與 migration 文件。
Web（Next.js／Phaser）及 scheduler 僅保留骨架。

M1：Agent、Project、Proposal、PrototypeRun、ApprovalRequest、BudgetReservation、
WorkflowEvent，以及 WorkspaceBudget、IdempotencyRecord、內部 MergeRequest。
提供提案建立／清單／詳情／修訂、CAS 版本狀態機、人類／worker 角色分離、
路徑與預授權檢查、核准期限與 revision/hash 綁定、預算保留與釋放、
冪等命令、原子 lease claim、交易式 audit/outbox 與 one-shot fake worker。

已實測的關鍵行為：

- 未授權／無憑證／未設定憑證／worker 冒用其他 agent 均拒絕。
- 無效 schema、路徑穿越、偽造 path prefix、非固定完整 SHA 均拒絕。
- 同 key 併發建立與啟動只產生一個 proposal/run/reservation。
- 不同 key 搶同 proposal 只成功一次；兩 worker 只取得一個 lease。
- agent／workspace 預算與併發限制有效，拒絕時業務交易整體回滾。
- 過期核准、修訂後 stale approval、過期／偽造 lease 均拒絕。
- 執行失敗、fake QA 失敗／偽造可信結果、清理失敗皆留下失敗紀錄。
- 取消會釋放保留預算；錯誤文字不會把測試用 secret 寫入 evidence/audit。
- 重啟 app 後 queue 與成功命令的 idempotency response 可恢復。
- audit envelope 符合 v1 JSON Schema，業務更新與 outbox 同交易。
- 人類 ACCEPTED 只建立內部待核准 intent，human／worker 的 merge/deploy 都回 403。
- one-shot worker entrypoint 實際透過 API 完成 fake 流程。

**102 個通過是治理程式的真實 Pytest 結果。** M1 的 CodingRuntime、SandboxManager、
ArtifactStore、TestRunner 是 fake，不會讀寫真實 repository、跑目標程式測試或呼叫 Codex。
Fake evidence 保持 `verified: false`、`command: null`、`exit_code: null`。
角色敘述、fixture PASS 與實際執行的測試證據不混用。

## 實際執行命令

最後一輪在原始專案執行的 PowerShell 核心命令如下。依賴使用工作區內下載並固定版本的
packages，沒有修改使用者的系統 Python。`requirements.lock` 是本次使用的 exact pins。

```powershell
Set-Location 'D:\Code\作品集\hogwarts-living-agent'
New-Item -ItemType Directory -Path 'test-results' -Force | Out-Null
$env:PYTHONPATH = 'C:\Users\User\Documents\Codex\2026-10-23\new-chat\work\hogwarts-deps'
$env:TEST_DATABASE_URL = 'postgresql+psycopg://hogwarts_test@127.0.0.1:55432/hogwarts_m1_test'
& 'C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m ruff check . --no-cache
& 'C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m ruff format --check . --no-cache
& 'C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -v --junitxml=test-results/junit.xml
```

檢查時各命令的 stdout/stderr 另以 `Tee-Object` 保存至
`test-results/ruff-check.txt`、`ruff-format.txt`、`pytest.txt`；JUnit 為 `junit.xml`。
完整檔案在交付壓縮檔的 `evidence/`，不只節錄摘要。

PostgreSQL 取得與啟動：從 EDB 官方 binaries 頁面取得 16.15 Windows x86-64 archive
（`https://sbp.enterprisedb.com/getfile.jsp?fileid=1260623`），解壓至工作區，
執行 `initdb -D work/postgresql16-data -U hogwarts_test --auth=trust --encoding=UTF8 --locale=C`，
並以 `postgres.exe -D <工作區測試資料夾> -h 127.0.0.1 -p 55432` 啟動。
這是臨時 localhost 測試 cluster，沒有掛載現有正式 DB。trust 僅用於此臨時測試，
不是交付的 Compose 預設；交付 Compose 要求明確設定密碼。

初始化工具在此 Windows restricted-token 環境印出 restricted token 訊息，
但 initdb 最終 exit 0 並完成 bootstrap。`pg_ctl start` 無法建立巢狀 restricted token，
因此直接在相同現有沙箱權限下啟動 `postgres.exe`；server 實際接受連線，
後續 50 個 PostgreSQL 案例皆通過。測試完成後，`pg_ctl stop` 的 signal 也受到
權限阻擋，改為依明確 executable path 停止本次建立的 PostgreSQL processes；
`pg_isready` 已確認 127.0.0.1:55432 不再回應。未觸及其他 DB processes。

## 環境問題與修正紀錄

- 系統 PATH 的 Python 是 3.10；改用 bundled Python 3.12.14。
- 初始 venv/ensurepip 與 pip 暫存目錄受 Windows mode 0700 ACL 相容性影響；
  開發依賴改裝到工作區，暫存目錄使用一般繼承 ACL。測試 tmp_path 也使用
  專案內明確目錄並於測試後清除。沒有將此安裝工具混入產品。
- M0 首輪 2 passed；M1 首輪 48 passed / 46 skipped（尚無 PostgreSQL）；
  PostgreSQL 就緒後 98 passed；補入清理失敗及 agent 冒用案例，最後原始目錄實測 102 passed。
- lint 初輪有 import、format 與 Security default 警告，已修正並重新執行通過。
- 保留原始 event.schema.json，以真實 audit events 驗證 envelope、event_type 與時間格式。
- Docker Desktop 因自己的設定檔寫入權限無法啟動。未繞過保護、未宣稱容器建置通過。

## 精確剩餘工作與風險

1. 在正常 Docker Desktop 環境完成 `docker compose up --build`，驗證 image build、
   migration service 啟動順序與容器 health；本次僅 Compose config 通過。
2. 將程式推至 GitHub 後才會執行已提供的 Actions；目前本次變更尚未推送。
3. M1 採本機單 owner 的環境變數 bearer tokens，沒有 production login、多租戶身分提供者、
   DB row-level security 或防竄改 audit 儲存，不能直接當作正式對外服務部署。
4. M1 預算是 in-flight reservation cap，fake actual cost 為 0；沒有每日累計實際付費帳務。
   在啟用真實付費 runtime 之前必須補上每日與實際成本對帳。
5. 過期 lease 被阻擋，但沒有自動回收／heartbeat／retry。可由人類 cancel 釋放保留；
   自動 reconciler、孤兒 sandbox 清理屬 M2。
6. M1 路徑僅做 canonical lexical 驗證；不宣稱能防真實 filesystem symlink escape。
   M2 必須實作固定 Git snapshot、真正隔離、檔案／網路／資源限制與工件保存。
7. M3 才驗證並選擇官方 Codex SDK/App Server；M4 goal/social kernel、M5 世界 UI 未實作。
8. 原始目錄 `.git` 仍受此環境保護，本次只將程式寫回來源；不宣稱來源 Git 已提交或連接遠端。

後續里程碑是 M2。必須先檢視本報告與真實測試證據，再另行開始；本次停在 M1。

## 交付工件

- 完整原始碼、frozen migration、CI／Compose、exact dependency pins。
- `contracts/openapi.v1.json` 與 `docs/07-API.md`。
- `docs/06-DEVELOPMENT.md`、ADR 0001／0002、本報告。
- 壓縮檔內 `evidence/junit.xml`、`pytest.txt`、Ruff 輸出、`MANIFEST.sha256`。
- 完整變更檔案清單另外列在交付的 `CHANGED-FILES.txt`。
