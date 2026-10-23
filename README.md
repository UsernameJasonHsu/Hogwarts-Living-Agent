# Hogwarts Living Agent OS — Specification Pack v0.4

**Status:** architecture/design baseline, not an implemented application.  
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
