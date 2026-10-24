# Hogwarts Living Agent OS — M2.1 交付與驗證

日期：2026-10-24（Asia/Taipei）。分支：`m2-1-git-workspace-manager`，本次未 Commit／Push／Merge。

**判定：M2.1 內部 Workspace Manager 功能驗收通過；Windows 原生 symlink 保留平台限制。**
Linux 全套 209 passed／0 failed／0 skipped；Windows 207 passed／
0 failed／2 skipped。兩邊都包含完整 M1 116 項 regression，全部通過。
未開始 M2.2–M2.4，未接入真實 Codex Runtime，也未將 Worktree 當作安全沙箱。

## 現有實作檢查與設計

已讀 AGENTS、README、PRD、System Design、Governance、Roadmap、既有 ADR 及 Adapter Interfaces。
M1 實際基準是修復後 116 項；GitHub commit 042db63 的 CI 已完成 success，證據保存在
evidence/m1-baseline-ci.json。這不代表未 Push 的 M2.1 有遠端 CI 證據。

沿用 Agent／Project／Proposal／Run／Lease／WorkflowEvent，不重建 M1 授權、狀態機與 claim。
M1 ArtifactStore 是 fake protocol、run evidence 內嵌資料，沒有 Artifact 資料表。M2.1 新增
GitWorkspace lifecycle（migration 0003），Diff 回傳 immutable bytes、SHA-256 與 changed paths，
只保存 provenance/checksum metadata；未擴張到 object storage、QA runner 或 Runtime。

介面在 workspaces/contracts.py，實作在 manager.py／git.py；設計見 ADR 0004，操作見 docs/10。
RepositoryGrant 是可信 operator mapping，Principal 來自既有可信認證，不能從 agent JSON 製造權限。
create/export 要求已授權、CLAIMED 的 M1 run、指定 worker 及有效 lease。Cleanup 允許原 worker
於 lease 過期後處理，或由 owner human 於取消後處理。沒有新增 HTTP endpoint，也沒有取代
M1 的 FakeSandboxManager。元件已可透過內部介面使用，自動 Worker 接線未納入此階段。

## 已實作功能

- 完整 lowercase SHA-1、object type=commit、明確 repository identity／local grant／path 交集。
- Prototype run 專屬 UUID 目錄，detached Git Worktree；不寫 source index／tracked files／branch ref。
- staged、unstaged、new、deleted、binary、ignored files 的完整 Diff；private index 不污染原 index。
- Changed Files 全量檢查；越權時不回傳部分 patch；可用 Git apply --check 驗證匯出的 patch。
- CREATING 意圖先持久化，Run unique constraint／DB write lock 序列化跨 manager 操作。
- READY 重試、CLEANED tombstone、重複清理、FAILED／CLEANUP_FAILED、可重試安全清理。
- 可接管正確且乾淨的 CREATING worktree；部分配置失敗有紀錄並可清理；未知衝突保留不盲刪。
- 重用 WorkflowEvent，source=git_workspace、aggregate_id=run_id，保存 stable error codes。

## 實際測試與證據

| 項目 | 結果 |
| --- | --- |
| 原始專案 Windows／SQLite + PostgreSQL | 207 passed、0 failed、2 skipped；286.01s |
| Docker Linux／SQLite + PostgreSQL | 209 passed、0 failed、0 skipped；115.94s |
| 既有 M1 regression | 每平台 116 passed；原測試斷言與 Fake Adapters 未修改 |
| M2.1 測試 | 93 項，含純 path cases 與兩種 DB 的實際臨時 Git repos |
| Ruff | check 與 format check exit 0 |
| Migration | 0003 head；SQLite／PostgreSQL upgrade/down/up 與 drift check 通過 |
| Docker | build/up、HTTP health、非 root API、test DB、test suite、清理全部 exit 0 |
| 分支保護 | main 仍為 042db63；工作在本機 M2.1 分支，沒有 Push 或 Merge |

實際命令與環境見附帶 verify.ps1：在原始專案跑 `python -m pytest -x -v --junitxml=...`，
Docker 內跑 `python -m pytest -v --junitxml=/tmp/m2-1-junit.xml`，並複製 Linux JUnit。
使用專用 Docker project `hogwarts-m2-1-207e7aca`，API 18084／測試 PostgreSQL 55435。
兩套 DB fixture 每案建獨立 schema，測試以自己建立的臨時 repo 當 workload，不使用真實專案當 workload。
Docker API 為非 root；一次性測試容器用 root 以寫入測試暫存檔，不能當作 M2 sandbox 安全性證明。

完整 JUnit、stdout、migration、health 與清理紀錄在 evidence/。既有 TestClient httpx deprecation
warning 未隱藏。首輪 Windows fixture 遇到 Git objects 唯讀、隱藏 .git file 的寫入差異；只修正
測試清理與 deliberate-tamper 寫入方式（r+b），沒有移除測試 assertion。首輪日誌保留在 attempt1/。

## 安全檢查

| 檢查 | 證據／邊界 |
| --- | --- |
| 不改來源正式分支 | 臨時 main／source index／source files 不變；真實 main ref 不變 |
| 不自動 Push／Merge | Manager 沒有這些操作；Git 只用固定 argv、shell=False，SHA／UUID／路徑先驗證 |
| 路徑穿越與不安全名稱 | ../、絕對路徑、反斜線、ADS、Windows devices、尾點、.GIT、大小寫歧義等拒絕 |
| Git symlink／gitlink | 讀取 Git tree mode，在 checkout 前拒絕；Windows／Linux 皆通過 |
| 工作目錄 symlink／hardlink | Linux 實際 links 與 sentinel 測試通過；Windows hardlink 通過、native symlink 跳過 |
| Metadata tamper | gitdir/backlink 與 pinned detached HEAD 檢查通過；不開啟被改寫的任意 Git 指標 |
| Repository config／hooks | includes、filter、alternate stores 拒絕；checkout hook 未執行、子程序環境限制 |
| Changed paths | 含 ignored 檔案全量驗證；越權／匯出期間變更均拒絕 |
| Audit／失敗 | timeout、配置失敗、配置後失敗與 cleanup failure 有持久狀態／事件；不記錄 token／raw stderr／patch |

Allowed Paths 是操作前授權及輸出檢查，**不是 OS 寫入限制**。服務不執行不可信程式；root 與
Git metadata 必須由可信服務持有，不能對惡意同權限程序宣稱 race-proof 隔離。Git administrative
worktree/object metadata 會改變，但沒有 source branch commit／index／tracked file 修改。

## 限制與未驗證項目

1. Windows 原生 symlink 建立不可用，以下兩案跳過：test_working_links_denied_without_touching_outside[sqlite-symlink], test_working_links_denied_without_touching_outside[postgresql-symlink]。
   Linux 的對應 SQLite／PostgreSQL 案例實測通過；不能宣稱 Windows native symlink 已通過。
2. NTFS junction 未另外建立原生整合案例；程式有 reparse-point guard，但不以靜態 guard 充當實測。
3. 僅支援普通 local non-bare SHA-1 repos；不支援 submodules、tracked symlinks、Git config includes／
   filters／alternates、.gitattributes、.gitmodules、.lfsconfig。限制會明確拒絕，不靜默忽略。
4. 併發測試是兩個 manager、獨立 DB sessions／threads；沒有另做 OS 多程序壓力／崩潰注入測試。
   Git+DB 無法同一原子交易；有 durable intent 與安全重入，未知部分狀態仍需 operator 處理。
5. M2.1 branch 未 Push，遠端 CI 未驗證；M1 舊 commit 的 CI success 不當作 M2.1 CI 通過。
6. 真實 Runtime、安全容器／MicroVM、網路／CPU／記憶體隔離、背景 lease/orphan reconciler、
   真實 artifact storage／發布均未實作或驗證；屬後續階段，本次未開始。

## 交付與完成判定

完成 M2.1 的介面、實作、ADR、migration、測試及本機跨平台證據；**功能驗收通過，保留上述
平台與遠端驗證限制**。下一階段不自動開始。所有變更保留在本機開發分支，沒有自動 Commit／Push。
新增／修改清單見 evidence/changed-files.txt，完整 diff 見 evidence/m2-1.diff；交付包含 source、
JUnit、raw logs、verification scripts、SHA-256 manifest。Git 主分支沒有變更，測試資源已清理。
