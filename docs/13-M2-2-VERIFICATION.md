# M2.2 Secure Sandbox Runtime — 驗收報告

日期：2026-10-24。判定：**指定範圍內的 M2.2 本機 Linux 驗收通過**。
此判定不代表任意第三方程式的安全認證、M2.3/M2.4 完成或遠端 CI 通過。

## Git 與既有工作保留

- 分支：`m2-1-git-workspace-manager`，沿用既有分支；沒有 checkout/reset/clean/stash。
- HEAD：`042db638355a246ea78c1b132497a98a6d0939f5`，與開工前相同；沒有新 commit/push/merge/deploy。
- 原先 M2.1 的未提交變更完整保留；56 個原檔案位元組指紋相同。
- models.py 只新增 SandboxInstance；AST 比對證實所有既有 import/function/class 定義一致。
- M1 service/auth/adapters、M2.1 workspace 及既有 regression assertions 均未改動。
- 本次新增/修改清單見 evidence/changed-files.txt；差異見 evidence/m2-2.diff。

## 設計與已實作功能

先讀取 AGENTS.md、起始文件、PRD/System Design/Governance/Roadmap、ADR 0004、
docs/10、M2.1 interfaces/models/implementation/tests；先交付 ARCHITECTURE-AND-TEST-PLAN.md
與 ADR 0005，再實作。最終架構及操作見 docs/12-SECURE-SANDBOX.md。

可信 Linux Controller 使用固定 Docker CLI argv、最小環境及已批准的 exact image ID；
Agent 只可選 profile 名稱，不能指定 shell、env、image、mount 或 Docker 參數。
重用 M1/M2.1 Run/Workspace/Lease/owner 授權、Git grant intersection 及 provenance。
支援 create/start/execute/stop/destroy、版本化生命週期、單次命令冪等、持久化意圖、
失敗與拒絕 Audit，以及身份驗證後的冪等清理。未知執行狀態禁止自動重播。

工作容器及可信初始化容器均 UID/GID 10001，read-only rootfs、cap-drop ALL、
no-new-privileges、network=none；無主機掛載、Docker Socket、host namespace 或憑證。
工作容器輸入為受限 Docker tmpfs volume 的唯讀掛載，輸出只進受限 /output、/tmp；
不將 Git worktree 當成 sandbox、不將 Git metadata 掛入容器，也不回寫來源 worktree。
CPU/RAM/PID/時間/暫存空間受限，期限不超過 Proposal 與原始 Lease；PID1 有獨立絕對期限。
初始化 stdin tar 拒絕 traversal/link/duplicate/oversize，完成後刪除初始化容器。
實作限定 image-owned deterministic probes，不執行 repository scripts 或真實 Codex。

## 真實測試結果

| 環境 | passed | failed/errors | skipped | 時間 |
|---|---:|---:|---:|---:|
| Windows 開發宿主＋SQLite/PostgreSQL | 273 | 0 | 22 | 333.493s |
| Linux Controller＋Docker Linux daemon＋SQLite/PostgreSQL | 295 | 0 | 0 | 201.235s |

- M1 regression：兩個環境各 **116 passed**。
- M2.1 regression：Linux **93 passed**；Windows **91 passed / 2 skipped**。
- M2.2：Linux **86 passed**；Windows **66 passed / 20 skipped**。
- Windows skips：18 個 Linux Docker integration、2 個 M2.2 native symlink、2 個既有 M2.1 native symlink。
  Linux 均實際執行，不能把 Windows skips 當成 Windows runtime 通過。
- Ruff check、format check 通過；Migration 0004 的 SQLite/PostgreSQL upgrade/down/base/up、
  Alembic drift check 通過（完整測試中的 migration tests）。
- 單一既有 Starlette/TestClient deprecation warning；無測試失敗。

Linux 真實測試包括非 root/提權拒絕、CapEff=0、NoNewPrivs=1、Seccomp=2、無網路路由、
唯讀 root/input、/dev/shm 拒寫、scratch 可寫、credential/socket 不可存取、限定 input paths、
CPU cgroup quota 及實際 throttling、PID fork 限制、tmpfs ENOSPC、記憶體超額 exit 137、
命令 timeout、容器停止（含 descendants）、獨立 absolute lifetime、實際建立/清理。
另有兩個以真實 inspect 為基礎、注入不安全 configuration 的拒絕測試；這是 validation
邏輯驗證，並未真的啟動 privileged/host-network 工作容器。

## 實際命令與證據

```text
python -m ruff check . --no-cache
python -m ruff format --check . --no-cache
docker build -t hogwarts-m2-2-sandbox:test <repo>/infra/sandbox
docker build --no-cache -f work/m2-2/Controller.Dockerfile -t hogwarts-m2-2-controller:test <repo>
python -m pytest -v --junitxml=<evidence>/windows-full-junit.xml
docker run --name <dedicated-controller> --network <dedicated-test-network>
  -v /var/run/docker.sock:/var/run/docker.sock
  -e TEST_DATABASE_URL=<dedicated-test-db; credentials omitted>
  -e HOGWARTS_SANDBOX_IMAGE=<approved sha256 image ID>
  hogwarts-m2-2-controller:test python -m pytest -x -v --junitxml=/tmp/m2-2-junit.xml
```

Socket 只提供給可信測試 Controller；它不是工作容器。實際 workload 的 inspect/tests
驗證沒有 socket/bind mounts。verification scripts 保存完整 argv 生成方式；密碼只在
臨時 private state/程序環境中使用，未放入交付包。Dedicated DB/network 於測試後移除。
JUnit、stdout、build logs、lint、初始/最終 Git 與檔案指紋都在 evidence/。
可信 Controller 測試映像使用先前已驗證的本機 M2.1 API 映像作 dependency base，再複製
本次原始碼及 Docker CLI；在其他主機可直接依 docs/12 在 Linux 安裝 requirements 後測試。

## 開發期間失敗與修正

1. Docker cp 無法寫入 read-only rootfs：改用非 root 初始化程式與受限 tmpfs volume，
   工作容器仍保持 read-only input/rootfs。保留 attempt1/diagnostic 證據。
2. Docker inspect Ulimits 順序不保證：改為依 Name 比較相同限制，未放寬任何值。
3. 非 root 探針 stat /root/.ssh 得到 PermissionError：改為明確判斷不存在或不可存取，
   不聲稱從 PermissionError 推導該路徑不存在。
4. BuildKit 缺失 parent snapshot：無清除使用者 cache；以 no-cache 重建成功。
   最終 build wrapper 檢查 exit code，禁止失敗時使用舊映像作為新版本證據。
5. 原生命週期測試的 3s 包含初始化，負載下初始化先到期，Runtime 正確拒絕；測試改為
   10s 並依 persisted absolute expiry 驗證自行停止，保留原失敗完整輸出。

上述歷史失敗不合併到最終成功數，也不刪除。最終 JUnit 才是完成判定依據。

## 限制與未驗證

- 遠端 GitHub Actions **尚未驗證**；CI 已配置 build pinned image + real Linux tests，未 push。
- Windows Runtime **不支援**；Linux 結果來自 Docker Desktop Linux kernel，非裸機 Linux 或 Windows 原生隔離。
- Container 共享 kernel，不是 MicroVM；未驗證 kernel escape、任意 hostile third-party code、
  user-namespace remapping、SELinux/AppArmor 特定 policy 或多租戶強隔離。
- 未做 host reboot、完整 process-crash/DB-outage 矩陣、inode exhaustion 實壓或跨 OS process 競爭壓測；
  concurrency evidence 使用獨立 manager/session 的 threads。未宣稱這些通過。
- 沒有 M2.4 Reconciler/即時 lease 撤銷 watchdog；original expiry/PID1/command timeout 提供上限，
  controller crash 後 metadata/volume 仍可能需要 owner 手動 destroy。
- Docker/DB 非原子；不確定 CREATING/EXECUTING 僅允許清理，可能需要人工排障。
- 已知 credential paths fail closed，但無通用 source-secret detection；operator 必須審核授權資料。
- M1 API 仍用 Fake Adapters。M2.2 是可信內部 service，沒有新增對外 endpoint 或自動導入真實 Runtime。
- scratch prototype 不同步回 Git；沒有 real Artifact/QA Store、Codex、M2.4、merge/push/deploy。

## 建議與完成判定

M2.2 的 Linux deterministic runtime/controller 在本次定義的安全與生命週期範圍內驗收通過。
保留既有未提交工作並交付可 review diff；下一步應由人類 review 後決定 commit/push，
再取得遠端 CI 證據。更強隔離或後續 M2.3/M2.4 需要另行授權與設計，沒有自動開始。
