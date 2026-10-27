# Hogwarts Living Agent OS — Specification Pack v0.4

**Status:** M1 / M2.1–M2.5 implemented; M2-RC1 security and CI remediation under evidence-based release gates. **M3 NOT READY.** See [current and historical status](docs/20-M2-IMPLEMENTATION-STATUS.md) and [RC1 operations](docs/29-RC1-RELEASE-GATES.md).
**Date:** 2026-10-23  
**Language:** zh-TW. Technical identifiers in English.

## Product vision
一座可探索的 2D 等角魔法學院，同時是能自主研究、在隔離沙箱製作軟體原型、交付可驗證成果的 AI 組織。

### Confirmed decisions
1. 工作與生活並重：世界事件與工程事件相互映射，但事實來源分離。
2. 2D 等角可探索學院：以 Next.js + Phaser 作為暫定實作方案。
3. 初期只支援軟體工程任務。
4. Goal-driven autonomy with schedule/event constraints.
5. **Autonomy B:** Agent 可自行研究和在**預先授權**的隔離沙箱製作 Prototype；正式合併、部署和擴權需人類批准。
6. 一開始採 Modular Monolith + Worker，不預設多微服務。
7. 角色人格與能力、授權完全分離。

### Document index
- `docs/01-PRD.md` — 需求、使用者故事、MVP 與非功能需求
- `docs/02-SYSTEM-DESIGN.md` — 模組、資料流、生命週期、恢復策略
- `docs/03-AGENT-SPEC.md` — 角色、目標計分、生活與工作機制
- `docs/04-GOVERNANCE.md` — 權限、成本、沙箱、人工審批
- `docs/05-ROADMAP.md` — M0–M5、驗收測試與交付條件
- `contracts/prototype.schema.json` — Prototype Proposal JSON Schema
- `contracts/event.schema.json` — 事件信封 JSON Schema
- `config/autonomy.example.yaml` — 自治政策初始範例
- `AGENTS.md` — Codex 專案根指示
- `CODEX_START_HERE.md` — 可以直接貼給 Codex 的起始任務

## Non-goals in MVP
完整 3D、語音即時對話、多使用者 MMO、未受限自主修改正式專案、自動合併到 protected branch、自動正式部署、Agent 自主取得管理者權限。

## IP note
霍格華茲及人物名稱屬概念研究中的既有世界觀引用；若公開商業化，應改為原創世界觀、角色、美術與品牌，或取得適當授權。核心程式用穩定 agent_id，顯示名稱可替換。

## Working principle
不能以 Agent 的敘述作為「完成」證據。實際工件、Git Diff、受信任測試工具輸出、審批記錄才是工程事實。

## Reading the timeline

Current schema head is0007. Each milestone section below records its original scope/exclusions; it is not a current global absence claim. The latest completed pre-RC1 baseline is Linux1471 and Windows1309+162 skips. RC1 results require the new source-bound manifest and raw report; remote CI is still unverified.

## M0/M1 implementation

- `apps/api/hogwarts`: FastAPI API、typed schemas、授權、CAS 狀態機與交易式事件紀錄。
- `infra/migrations`: M0 foundation 與 M1 governance schema；PostgreSQL 為正式資料來源。
- `workers/engineering/worker.py`: 明確指定單一工作的 fake worker，沒有自治迴圈。
- `tests`: SQLite 與 PostgreSQL 的 migration、權限、冪等、併發與 fake workflow 測試。
- `compose.yaml`、`infra/Dockerfile`、`.github/workflows/ci.yml`: 本機與 CI 設定。
- [開發與環境設定](docs/06-DEVELOPMENT.md)、[API 說明](docs/07-API.md)、[決策紀錄](docs/adr/0002-m1-governance.md)。

M1 使用 Fake Adapters；後續 M2 的 Workspace、Sandbox 與 QA 功能見下方。Fake QA
明確標記 `verified: false`，不代表目標專案的測試已通過。此歷史 M1 基準之後已實作 M2.3.5、M2.4 與 M2.5；M3 尚未開始。

M2.1 新增內部 Git Workspace Manager；介面、安全限制與操作方式見
[Git Workspaces](docs/10-GIT-WORKSPACES.md) 與 [ADR 0004](docs/adr/0004-m2-1-git-workspaces.md)。
Worktree 不等於安全沙箱，也未接入真實 Runtime。

M1 驗收發現的稽核與 token 處理問題已修正；設計細節見
[ADR 0003](docs/adr/0003-m1-acceptance-fixes.md)，複驗證據見
[M1 修復與複驗報告](docs/09-M1-FIX-VERIFICATION.md)。


## M2.2 Linux Secure Sandbox Runtime

新增可信內部 Controller、固定 Runtime Profiles、非 root Linux 容器隔離與
冪等生命週期；M1 HTTP API 及 Fake Adapters 保持原樣。Windows 僅為開發宿主。
操作與限制見 [Secure Sandbox Runtime](docs/12-SECURE-SANDBOX.md)、
[ADR 0005](docs/adr/0005-m2-2-secure-sandbox.md) 及 [M2.2 驗收報告](docs/13-M2-2-VERIFICATION.md)。
本階段沒有真實 Codex、Artifact/QA Store、背景 Reconciler 或正式部署。


## M2.3.1 Evidence Schema & Local Artifact Store

新增版本化 Evidence Schema、綁定 Run/Workspace/Sandbox 的本機不可變 ArtifactStore
及 Migration 0005。SHA-256 只證明完整性；Agent 上傳一律 unverified，沒有自動 verified
或 QA 判定。M1 fake evidence 與 M2.1/M2.2 安全行為保持原樣。
架構、介面與限制見 [Evidence & Artifacts](docs/14-EVIDENCE-ARTIFACTS.md) 與
[ADR 0006](docs/adr/0006-m2-3-1-evidence-artifacts.md)。後續已實作的 M2.3.2–M2.3.4 見下方。

[M2.3.1 驗收報告](docs/15-M2-3-1-VERIFICATION.md) 包含實際命令、證據與未驗證項目。


### M2.3.2 Secure Output Import

See `docs/adr/0007-m2-3-2-secure-output-import.md` for architecture, trust boundaries and the preimplementation test plan.
`OutputController` / `OutputDockerDriver` extend the Linux-only runtime with a bounded output volume and a separately pinned trusted readonly reader. `LocalOutputImporter` requires controller identity, current Run/Workspace/Sandbox lease and unchanged repository grants; it imports a stopped producer's authenticated fixed USTAR snapshot into the existing immutable ArtifactStore. No Agent-supplied host path, shell command, extraction, patch application or QA promotion is available. Imported evidence remains **unverified**.

Migration `0006` adds `output_imports` only. Build `infra/output-reader`, pin its image ID in `HOGWARTS_OUTPUT_READER_IMAGE`, and set `HOGWARTS_SANDBOX_IMAGE` to opt into real Linux tests. Windows executes parser, database and local storage tests; Linux runtime tests are explicitly skipped there. Remote CI is unverified until actually executed.

A snapshot intent with no authenticated capsule is busy/uncertain and cannot be recaptured on retry. Failed capture, invalid archive or capsule tampering fails closed. Artifact publication retries use only the sealed capsule and deterministic keys; partial artifacts remain unverified and no successful import manifest is returned until final DB commit. Destroy removes owned producer/input volume/reader/output volume; orphan reconciliation is outside this milestone.


### M2.3.3 Trusted Git Patch Builder

新增內部 `TrustedGitPatchBuilder` / `LocalGitPatchBuilder`，從已完成的 M2.3.2 Import Manifest 建立 add/modify-only text/binary patch。可信私有 Git repository 的完整 Base index 與第二個 apply index 驗證路徑、內容 SHA-256 及可套用性；不 checkout、不執行專案程式、不修改來源 branch/index/ODB、不推斷刪除或重新命名。Patch Apply Check 不代表 QA Passed，所有 Artifact 仍為 **unverified**。

沿用 M1 IdempotencyRecord 作版本化持久化 journal，沒有新增 DB migration；既有模型、安全規則與測試保留。操作與風險見 [Trusted Git Patch Builder](docs/18-TRUSTED-GIT-PATCH-BUILDER.md)、[ADR 0008](docs/adr/0008-m2-3-3-trusted-git-patch-builder.md)。遠端 CI 尚未驗證；不包含 M2.3.4/.5、M2.4、真實 Codex 或自動 commit/push/merge/deploy。


### M2.3.4 Trusted QA Runner

新增內部 `TrustedQARunner` / `LocalTrustedQARunner`、固定且由 operator 批准的 QA Profile Registry、
全新 Linux QA 容器與獨立 `QAExecutionAttestation`。重新驗證 Patch/BuildProof 並重建授權範圍的
Base+輸出快照；真實測試只在非 root、無網路、無主機掛載、受資源與期限限制的容器中執行。
命令結果以可信 Controller 及 Docker 觀測為準，輸出中的成功宣稱不具權威。

沿用 M1 IdempotencyRecord 保存版本化 journal，Alembic head 保持 0006；沒有 SQL schema 變更。
未知執行不得自動重跑，完整 HMAC 結果只允許重試 Artifact 發布。所有 Imported/Patch/BuildProof/QA
Artifact 保持 **unverified**；可信執行證明不代表內容、覆蓋率、獨立審查或合併已通過。
詳見 [Trusted QA Runner](docs/19-TRUSTED-QA-RUNNER.md) 與 [ADR 0009](docs/adr/0009-m2-3-4-trusted-qa-runner.md)。
Windows 僅為開發宿主；遠端 CI、M2.3.5、M2.4 與真實 Codex 未包含在本階段。


## M2.3.5 End-to-End Verification and Human Review

新增內部 PrototypeVerificationOrchestrator，串接既有 Workspace / Sandbox / Import / Patch / QA；產生不可變且維持 unverified 的 Review Bundle。Acceptance Gate 要求相同 Run、Base、Patch Snapshot、可信 QA Journal/HMAC 證據與固定映像的正數測試執行。

本次新增的受信任 QA Profile 僅驗證既有 deterministic producer fixture；映像持有兩個固定測試，零測試不得通過。一般 unittest exit 0 不足以進入人類審查。READY_FOR_HUMAN_REVIEW 的 Run 對應 REVIEWING Proposal；人類需核對 Bundle SHA、版本、授權及期限，接受只建立 Merge Intent。

操作與限制見 [Prototype Verification](docs/21-PROTOTYPE-VERIFICATION.md) 及 [ADR 0010](docs/adr/0010-m2-3-5-prototype-verification.md)。沒有新 SQL migration；Alembic head 保持 0006。完整實測驗收結果另附；M2.4、真實 Codex 與正式部署不在本次範圍。


## M2.4.1 Lease Heartbeat and Fencing

新增可信內部 DurableLeaseManager：持久化 heartbeat、generation/version CAS、撤銷與過期、發布交易 fencing 及可重試安全 Stop Hook。Migration 0007；既有證據維持 unverified。過期或 unknown execution 不會自動重播，也沒有 Resource Reconciler/GC。操作與限制見 [Lease Heartbeat & Fencing](docs/22-LEASE-HEARTBEAT-FENCING.md) 及 [ADR 0011](docs/adr/0011-m2-4-1-lease-heartbeat-fencing.md)。驗收狀態以本次實際測試報告為準；遠端 CI 與真實 Codex 未驗證。


## M2.4.2 Resource Reconciler

新增可信內部 ResourceInventory / IdentityVerifier / StateComparator / CleanupPlanner / CleanupExecutor，提供版本化 CleanupJournal、dry-run、KEEP / CLEANUP_ELIGIBLE / QUARANTINE、Run-first CAS / generation fencing、執行前重新 inspect 及精確 Docker ID / volume / registered worktree 清理。永久 Artifact、Review Bundle 與恢復證據保留；未知或不確定的資源不接管、不重播。沒有新增 SQL migration，head 保持 0007。詳見 [Resource Reconciler](docs/23-RESOURCE-RECONCILER.md) 與 [ADR 0012](docs/adr/0012-m2-4-2-resource-reconciler.md)；實測結果以外部驗收報告為準，遠端 CI 未驗證。沒有 Crash Recovery、Retention/GC、真實 Codex 或 SCM/部署動作。


## M2.4.3 Crash Recovery and Workflow Reconciliation

新增內部 CrashRecoveryCoordinator、WorkflowStateInspector、RecoveryClassifier、RecoveryPlanner／Executor、版本化 RecoveryJournal 與 Human Intervention Queue。預設 dry-run；只有已驗證的完整證據能透過 publication-only 入口重試發布，UNKNOWN workload 不重跑。合法終態與有充分 deterministic zero-cost 帳務證據的結算使用既有 M1 規則，Cleanup 仍由 M2.4.2 明確執行。既有 Artifact 保持 unverified；沒有新 SQL DDL，head 保持 0007。詳見 [Crash Recovery](docs/24-CRASH-RECOVERY.md) 與 [ADR 0013](docs/adr/0013-m2-4-3-crash-recovery.md)，真實測試結果以外部驗收報告為準。沒有 Retention/GC、真實 Codex、自動 commit/push/merge/deploy；遠端 CI 未驗證。

### M2.4.4 Retention, Quotas and Operational Readiness

Internal owner-controlled aggregate quota reservations, logical storage accounting, versioned retention, Holds, dry-run and explicitly approved ephemeral GC reuse M1 auth/fencing and the existing Reconciler. All permanent evidence stays unverified and retained. No automatic quarantine cleanup or UNKNOWN workload replay. See docs/25-RETENTION-OPERATIONS.md, docs/26-OPERATIONS-RUNBOOKS.md and ADR 0014. SQL head remains 0007. Controller admission is distinct from kernel/physical disk quota; deployment-wide wiring, remote CI, existing quarantine disposition and production readiness remain separate gates. No M3 implementation, real Codex or automatic SCM/deploy actions.


### M2.5 Operational Gate Closure and M3 Readiness Preflight

Canonical startup uses the explicit disabled/fake/operational factory. Operational HTTP dispatch requires a durable formal intent, existing governance/lease fencing, monetary reservation and aggregate quota admission before resource effects. Bound adapter/publication entrypoints reject direct bypass; cancellation and UNKNOWN preserve unconfirmed commitments. Existing unbound adapters remain trusted compatibility APIs, not Agent tools or an OS sandbox. See [formal entrypoints](docs/27-FORMAL-ENTRYPOINTS.md) and [ADR 0015](docs/adr/0015-m2-5-formal-entrypoints.md). CI now configures all six pinned Linux images and rejects skipped/incomplete suites; remote CI remains unverified until run. Four existing quarantine resources remain manual-only. No real Codex, M3 implementation, evidence promotion or SCM/deploy action. Actual acceptance and readiness remain subject to the delivered test evidence and unresolved operational gates.
