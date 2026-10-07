# 领域约定

保存生活经验专辑的地点版本、分享快照和事实纠错，让过期变化可见但不抹去原判断。

聚合对象包括 `experience_album`、`place_revision`、`shared_snapshot`、`correction_case`。
所有时间都必须携带时区，版本号从 1 开始递增，校验层不会替调用方改写输入。

## 事件类型与载荷

| 事件类型 | 必需载荷 | 说明 |
| --- | --- | --- |
| `ALBUM_CREATED` | — | 建专辑，actor 为作者 |
| `COLLABORATOR_INVITED` | `user_id` | 邀请协作者 |
| `ENTRY_ADDED` | `experience_at`, `audience_conditions` | 新增亲历建议，同时钉住 `place_id`/`place_version` |
| `ENTRY_UPDATED` | `entry_id` | 作者/协作者更新判断，须带 `expected_version` |
| `ENTRY_MERGE_RESOLVED` | `entry_id`, `base_version` | 并发冲突后的显式合并落点 |
| `PLACE_REGISTERED` | `place_id`, `place_version` | 地点建档（版本从 1 开始） |
| `PLACE_CHANGED` | `place_version`, `changed_facts` | 营业时间等事实变化；`removed_facilities` 表示设施撤除 |
| `PLACE_MERGED` | `old_place_id`, `new_place_id` | 门店合并，只迁移 `confirmed_entry_ids`/`confirmed_comment_ids` |
| `SNAPSHOT_SHARED` | `snapshot_hash`, `recipient_scope` | 冻结原文与地点版本的分享快照 |
| `COMMENT_ADDED` | `comment_id`, `entry_id`, `quote`, `place_id`, `place_version` | 评论引用原文并钉住地点版本 |
| `CORRECTION_SUBMITTED` | `case_id`, `subject_id`, `factual_patches` | 商家事实更正 |
| `CORRECTION_DECIDED` | `case_id`, `decision` | 审核员裁决，`accepted` 时落地事实事件 |
| `AUTHOR_WITHDREW` | `entry_id` | 作者撤回认可，快照原文不变 |

交换层（`contracts.py`）只负责稳定报告结构、枚举、时间、版本和必需载荷；
以下业务规则属于上层服务（`service.py` / `store.py` / `freshness.py`）。

## 业务规则

### 幂等与版本纪律

- 每个写命令必须携带 `request_id`；**相同请求只执行一次**，重试/重启重放返回首次事件。
- 同一 `request_id` 携带不同请求体 → `idempotency_conflict`。
- **同一聚合、同一版本号出现不同内容 → 冻结该事件流**（`version_frozen`），
  冻结后拒绝一切追加，只能人工排障解冻；同版本同内容视为重放。
- 条目编辑采用条目级乐观锁：`expected_version` 落后即 `merge_required`，
  协作者必须读取冲突中返回的当前状态，调用显式合并（`ENTRY_MERGE_RESOLVED`）。

### 快照与失效可见

- 快照冻结分享时刻的标题、推荐、理由、适用条件、负面体验、亲历时间、
  地点事实与地点版本，生成 `snapshot_hash`；后续任何变化都不改写快照。
- 阅读快照时对照当前地点状态返回 `staleness`：事实变化、设施撤除、停业、合并。
- 作者撤回认可只翻转 `author_endorses_now`，快照内 `endorsed_at_share` 与原文保留。

### 地点合并不吞体验

- 合并只迁移显式确认仍指向同一主体的条目与评论；未确认的引用留在旧门店，
  旧门店标记 `merged` 与 `merged_into`，针对旧门店的体验与评论仍可阅读、追加。
- 迁移引用必须当前确实指向旧门店，否则拒绝。

### 事实更正与审核回避

- 商家只能改事实键（`smoke_free`/`hours`/`facilities`/`location_note`/`contact`/`status`），
  触碰推荐、理由、负面体验、适用条件等观点字段一律拒绝；**商家不能删除用户观点**。
- 接受更正只追加事实事件，不动任何观点字段；裁决幂等，已裁决案件不能二次处理。
- **审核员不得处理自己参与过编辑的专辑**（作者、协作者、在该专辑落地过事实者），
  也不能裁决自己提交的申诉；违例为 `reviewer_conflict`。

### 新鲜度检查

- 时钟必须可注入（`Clock` 协议），禁止在检查逻辑中直接读系统墙钟。
- 通知种类：事实变化、地点失效（停业/合并）、亲历超龄、快照失效。
- 通知去重键精确到「接收人 + 条目/快照 + 地点版本号 + 变化细类」，
  台账持久化到磁盘，**重启后不重复通知**；同一地点出现新版本变化才会再通知。

### 阅读接口与个人信息

- 条目视图同时给出：`experience_at`（基于何时的体验）、
  `changed_facts_since_experience` + `accepted_corrections`（哪些事实已变化）、
  `author_endorses`/`withdrew_reason`（作者是否仍认可）。
- 证据附件摘要可标记 `contains_personal_info`；仅作者、同专辑协作者、
  审核员可见原文，其余角色看到存在性与遮蔽文案（`redact_attachments`）。
- 快照接收者只能读自己 `scope` 内或 `public` 的快照。
