# M2.3.1 Evidence Schema & Artifact Store — Acceptance Report

日期：2026-10-24。判定：**M2.3.1 指定範圍本機驗收通過**。
此判定只涵蓋 Schema、可信本機 byte store、授權、完整性與故障行為；不代表 QA/Verifier、
M2.3.2–M2.3.5、M2.4、真實 Codex 或遠端 CI 完成。

## Git 與原有修改

- 分支 `m2-1-git-workspace-manager`；HEAD `042db638355a246ea78c1b132497a98a6d0939f5` 與開工前相同。
- 沒有 checkout/reset/clean/stash/commit/push/merge/deploy。
- 68 個既有檔案 SHA-256 完全一致，包括所有既有 M1/M2.1/M2.2 安全模組、
  regression tests、CI 與原先未提交文件。models.py 只追加 ArtifactMetadata，AST 驗證所有
  既有定義不變；README 只更新階段狀態/新增說明。原始指紋與 Git status 均保存。
- 完整變更清單：evidence/changed-files.txt；本次差異：evidence/m2-3-1.diff。

## Architecture / ADR

開工先讀 AGENTS.md、README/PRD/System Design/Governance/Roadmap、ADR 0004/0005、
M2.1/M2.2 實作與測試、WorkflowEvent/PrototypeRun.evidence/fake ArtifactStore。
先輸出 ARCHITECTURE-ADR-TEST-PLAN.md 與 ADR 0006，再寫介面/本機儲存與測試。
操作文件在 docs/14-EVIDENCE-ARTIFACTS.md；ADR 在 docs/adr/0006-m2-3-1-evidence-artifacts.md。

架構：trusted internal ArtifactStore -> existing M1 owner/worker/lease authorization
-> SQLAlchemy Metadata + private LocalObjects。沒有新的 HTTP endpoint 或 Agent filesystem tool。
M1 inline fake evidence/Run.evidence 與 M2.2 Controller 完全不變，沒有自動接入或 artifact collector。

## 已實作

1. Frozen/extra-forbidden Evidence v1 與可匯出的 JSON Schema，版本同時持久化並納入 metadata hash。
2. ArtifactStore Protocol、ArtifactUpload、EvidenceDocument、ImmutableArtifact、ArtifactRead。
3. 本機 private objects store，server UUID 不可變 ID，原始 bytes SHA-256/長度及 canonical metadata SHA。
4. 綁定 Run/Workspace/Sandbox、原始 lease hash、producer、server time、base commit/input SHA。
   執行狀態/時間從 SandboxInstance 與相符 WorkflowEvents 取得；未知值保留 unknown/null。
5. Upload 只能指定 kind/media，不能指定 path/source/time/hash/verified；所有成功上傳一律 unverified。
6. verified/unverified/failed 定義清楚：verified 保留給未來可信 verifier，本次沒有 issuer/promote API；
   failed 是 storage/integrity failure，與 controller execution failed 分開。SHA 相符不代表報告為真。
7. Durable PENDING + Audit -> exclusive create/fsync/read-only -> READY + Audit。相同 key/body 回傳相同 ID；
   不同 body/type/media/bindings 衝突；並行獨立 Store/session 不產生重複 metadata。
8. DB final commit failure 後只可恢復完整相符檔案，重新 fsync；部分/不同 bytes 不覆寫、不自動修復。
9. 先驗證 owner/run/lease 再讀物件；worker lease 在讀取後再檢查，owner 可讀歷史資料。
10. UUID-only filenames、root 排除 worktree/repository、symlink/reparse/special/hardlink 拒絕、
    lstat/fstat 比對、bounded reads、大小/hash/metadata 驗證。未知/竄改內容不回傳部分 payload。
11. WorkflowEvent source=artifact_store，版本化 lifecycle events，無 raw content/token/key/exception。

## Migration

`0005_m2_3_1_artifacts.py`，down_revision=0004；新增 artifact_metadata。
包含 Run/Workspace/Sandbox foreign keys、owner/producer/lease、hashed idempotency key、request/content
hash/size、schema version、immutable JSON/hash、state/version/error。Unique(run, producer, key hash)，
Check state/size/schema version。既有模型/資料表授權規則沒有改動。
SQLite/PostgreSQL 的 upgrade -> base downgrade -> upgrade 及 Alembic drift check 皆在完整測試中通過。
沒有對正式 DB 執行 migration。

## Actual Test Evidence

| 環境 | passed | failed/errors | skipped | 時間 |
|---|---:|---:|---:|---:|
| Linux Controller / Docker Linux daemon / SQLite+PostgreSQL | 415 | 0 | 0 | 317.084s |
| Windows 開發宿主 / SQLite+PostgreSQL | 385 | 0 | 30 | 624.287s |

| Test group | Linux passed/skipped | Windows passed/skipped |
|---|---:|---:|
| M1 | 116 / 0 | 116 / 0 |
| M2.1 | 93 / 0 | 91 / 2 |
| M2.2 | 86 / 0 | 66 / 20 |
| M2.3.1 | 120 / 0 | 112 / 8 |

另針對補強後的「Lease 到期 + Sandbox/Worktree 清理後 owner 歷史讀取」案例，在兩平台各
重跑 SQLite/PostgreSQL **2 passed**，evidence/*-history-junit.xml。這是同名案例重跑，
不重複加入 415 個不同測試總數。主完整執行時使用此前已匯入的測試版本；補測證明最後新增的
清理後 assertion，production implementation 未變更。額外 testcase assertion 不冒充已在先前 full run 執行。

Windows 30 skips：原有 18 個 Linux Runtime + 4 個 symlink；新增 2 個真實 Linux Controller
association、4 個 file/directory symlink、2 個 POSIX FIFO。這些均在 Linux 執行；不能宣稱 Windows
原生 Runtime/FIFO/symlink 隔離通過。實際 native Windows hardlink、read-only recovery/fsync、
concurrent IO、tampering、DB rollback 皆執行通過。

Artifact 120 cases 涵蓋 create/read/describe、empty/text/binary、metadata/schema hash/version、
JSON Schema、伪造 verified 拒絕、immutability/replay/conflicts、separate Store concurrent writes、
cross-run/owner/worker/lease/cancel/binding 拒絕、traversal/absolute/ADS、unsafe roots、file/directory
symlink、hardlink、FIFO、missing/size/same-size/metadata tamper、partial publication、fsync/IO error、
DB before-intent/final-commit failure、DB+Audit unavailable、expiry during IO。兩個案例連到真實
M2.2 Controller，以實際執行成功資料驗證上傳仍為 unverified；其他 sandbox fixtures 使用 FakeDriver，
不能據此聲稱 kernel isolation。原有 M2.2 REAL Linux tests 也完整通過。

執行命令（DB credentials 僅在私有環境使用，不列入證據包）：

```text
python -m ruff check . --no-cache
python -m ruff format --check . --no-cache
python -m pytest tests/test_artifacts.py -x -v --junitxml=<focused evidence>
docker build --no-cache -f work/m2-3-1/Controller.Dockerfile -t hogwarts-m2-3-1-controller:test <repo>
docker run --name <trusted-test-controller> --network <dedicated-test-network>
  -v /var/run/docker.sock:/var/run/docker.sock
  -e TEST_DATABASE_URL=<dedicated PostgreSQL> -e HOGWARTS_SANDBOX_IMAGE=<approved exact M2.2 image>
  hogwarts-m2-3-1-controller:test python -m pytest -x -v --junitxml=/tmp/m2-3-1-junit.xml
python -m pytest -x -v --junitxml=<windows-full-junit.xml>
python -m pytest tests/test_artifacts.py -k reads_are_run_scoped_and_owner_historical -v --junitxml=<history evidence>
```

Socket 只屬於可信測試 Controller，沒有提供給 workloads。重用已核准 M2.2 runtime image；未改動
runtime code/profile/security。測試使用一次性 Git repos、DB schemas/private roots。可信 Linux test
image 基於上次已驗證的 M2.2 controller/dependencies，COPY 本次 source；scripts/approved image IDs/
build logs/source hashes/JUnit/stdout 均附於 evidence 或 verification/。

## Security Findings / Failure Findings

- Verified trust boundary：只靠 actor report、exit 0、JSON status 或 SHA 相符都不能取得 verified。
- Immutable storage：API 無 overwrite/delete；file readonly 是防誤寫，真正完整性依 trusted DB hash。
  POSIX private ownership + nofollow/fstat/single-link 與 Windows native checks 均有對應測試。
- Fail closed：部分檔案、內容/metadata 竄改、跨 run、非法路徑與 lease 到期不回傳內容；失敗保留 Audit。
- Honest audit：DB 完全不可用時可同時無法保存 Audit；穩定回傳 503 artifact_database_and_audit_unavailable，
  不宣稱稽核已持久化。未引入隱藏 fallback log 或暴露原始錯誤。
- 初始 Linux/Windows full run 都停在 DB commit fault injection：新增重查引發 autoflush，
  session.dirty 已清空，原注入器未觸發；測試明確失敗，不視為通過。修正為查看 identity_map，
  加上 assert triggered，並補上 partial PENDING 拒絕恢復。未減少任何驗收 assertion。
- 初始版本 Linux artifact-focused 108 passed；最終以完整 415 與歷史補測為準。所有歷史失敗輸出保留，
  不加進最終成功數。沒有尚未解決的本輪實際測試 failure。

## Unverified Items / Limits

- 遠端 M2.2/M2.3.1 CI 未驗證；沒有 push，沒有為它標記通過。既有 CI 會發現新增測試，未變更 CI。
- Schema 定義 verified，但本階段不實作可信 verifier/QA issuer；這是刻意 fail closed 的範圍限制。
- 無 artifact collector、diff/test/report semantic verification、real Codex、M2.3.2–M2.3.5、M2.4。
- 本機檔案 API 需 trusted service-owned root/DB；不抵抗同 OS UID/admin 惡意改寫、kernel escape、
  WORM/encrypted storage、分散式 store 或 Windows ACL race。沒有 native Windows junction integration test。
- 未做真正 host power loss/reboot、DB server/network outage、跨 OS process 壓測或 fsync 硬體持久性證明；
  故障注入使用真實 DB transaction/IO 與受控 exceptions，concurrency 使用獨立 Store/session threads。
- 只有 16 MiB/artifact 上限；total quota/retention/GC/backup-restore 未實作。root 必須保持固定，
  operator 應使用獨立有容量控制的 private filesystem。failed/orphan objects 可能需人工排查。
- 沒有通用 source/content secret scanner；不把 Store 掛入 workloads，Audit 不保存內容與 token。
- 原有 TestClient deprecation warning 仍在；未改動 M1 依賴以避免擴大範圍。

## Acceptance

M2.3.1 在指定的本機 Evidence Schema/ArtifactStore 範圍驗收通過；所有既有 regression 保留並執行。
交付可 review source、Migration 0005、ADR、diff、指紋與真實證據。下一步由人類 review 決定是否
commit/push 取得遠端 CI；本次不自動進入後續階段，也不修改正式部署環境。
