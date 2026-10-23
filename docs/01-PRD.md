# PRD — Hogwarts Living Agent OS v0.4

## 1. Vision & target audience
個人開發者進入 2D 等角魔法學院，將真實軟體需求交給虛擬 AI 組織；角色也能在不受新指令驅動時，根據目標與證據自主提出技術研究，在受限沙箱內製作 Prototype，最後由人類批准進入正式整合。

**Primary persona:** 熟悉 Git 的個人開發者與作品集創作者。  
**Core value:** 看得見的 Agent 社會 + 可驗證的工程產出 + 可追溯的人類治理。

## 2. Locked product decisions
- 工作與生活並重；所有社交、精力、壓力都是**模擬參數**，不可造成故意降低程式品質。
- 2D isometric scenes：大禮堂、校長室、圖書館、萬應室；儲思盆可先以側邊面板呈現。
- 初期任務只限既有 Git repository 中的小型軟體變更。
- Goal-driven with schedule constraints and deterministic policy gates.
- Agent 自主 research、propose、sandbox prototype；禁止自行 merge、deploy 或 elevation。

## 3. User journeys
### UJ-01 Founder commissioning
1. 使用者註冊/登入，授權一個 repository 和可操作範圍。
2. 在校長室描述需求，取得 Mission ID。
3. 議會產出目標、驗收條件、風險和任務圖。
4. 人類批准超出預授權範圍的工作。
5. 工程 Worker 於隔離沙箱完成變更；QA 與 Reviewer 產生證據。
6. 使用者查看 patch、測試、風險與預覽，選擇接受、拒絕、要求修改或暫緩。
7. 接受只會產生 Merge Request；不直接合併。

### UJ-02 Autonomous prototype
1. 角色從**已授權** repository 的檔案、測試及歷史發現改善候選。
2. 唯讀研究提出證據、受影響檔案、風險和驗收標準。
3. Governance 根據項目授權、路徑、成本、排程及並行數判定是否允許 sandbox execution。
4. 使用固定 base commit 建立 sandbox，Codex Adapter 執行限域程式修改。
5. QA Runner 實際跑測試、Reviewer 檢查成果。
6. 展示可追溯的原型；工作成功/失敗都會記錄到世界事件和儲思盆。
7. 等待人的整合決定。

### UJ-03 Living world
1. 角色在房間間按 schedule 移動；世界 renderer 只顯示 Event Store 投影。
2. 允許的社交互動可產生候選提案；聊天觀點不得當成工程事實。
3. 使用者進入儲思盆，按 Task/Agent/事件追查歷史證據。

## 4. MVP features (must)
- 可登入的單使用者工作區；單 repository 授權與移除。
- 七位角色的身份與角色狀態；四位首先實現目標驅動行為。
- Goals: generation, scoring, deduplication, cooldown, prioritization, commitment.
- 原型提案 CRUD、狀態機、人工批准與拒絕。
- 完整 workflow event log，重啟能恢復狀態。
- Git base commit 鎖定、沙箱隔離、Patch 輸出、測試報告、獨立審查。
- 2D 地圖（4 rooms）、角色位置、任務映射、可選的生活對話。
- 成本預算、併發限制、緊急暫停與可稽核紀錄。

## 5. Excluded from MVP
3D、語音、無限制多 Agent 自我繁殖、無限制網路瀏覽、直接存取正式 DB、正式環境部署、未核准 protected branch merge、完整多人遊戲、真正意識/情緒宣稱。

## 6. Quality attributes / measurable acceptance
- 所有權限拒絕及狀態機轉移以 unit/integration tests 驗證。
- workflow 重送命令不得創建重複執行（idempotency key）；競爭的 worker 只能一個取得 lease。
- Agent 未核准的 write/merge/deploy 被執行層擋下；測試要包含惡意工具輸出和提示注入樣本。
- 每個 Prototype 有固定 base commit、Patch、Test Run、Reviewer Report、provenance、cost record 或明確失敗理由。
- 世界 UI 不能把 simulation event 說成 code/test verified event。
- 重啟後能恢復 queued/in-progress 狀態並清理孤兒沙箱。
- 使用者可暫停新任務並要求取消執行；不能承諾強制停止已不可中斷的外部操作。

## 7. Success KPIs (targets, not actuals)
- 在範例 repo 上端到端完成 1 個未影響 main 的原型。
- 所有合併均有對應人類批准紀錄。
- QA 與程式事實 100% 可點擊追溯到可信任的執行輸出。
- 自主提案重複率及每任務成本可量測，先建立基準後調整門檻。

## 8. UX rooms
- Great Hall：活動和各 Agent 狀態總覽。
- Headmaster Office：建立需求、待批准通知、決策摘要。
- Library：技能與文件、證據、研究提案。
- Room of Requirement：Sandbox 任務進度、Diff、測試與成果。
- Pensieve panel：可搜尋事件、決策、記憶及鏈結。

## 9. Product/legal assumptions
既有知名角色是早期 prototype 的角色設定範本，未取得 IP 授權前不公開商業化這些品牌/外觀。最終 UI 圖像使用原創資產或具適當授權的素材。
