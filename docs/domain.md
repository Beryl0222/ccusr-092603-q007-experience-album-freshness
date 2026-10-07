# 领域约定

保存生活经验专辑的地点版本、分享快照和事实纠错，让过期变化可见但不抹去原判断。

聚合对象包括 `experience_album`、`place_revision`、`shared_snapshot`、`correction_case`。所有时间都必须携带时区，版本号从 1 开始递增，校验层不会替调用方改写输入。

## 事件

| 事件 | 聚合 | 含义 |
| --- | --- | --- |
| `ALBUM_CREATED` | experience_album | 建专辑，含作者身份声明 |
| `COLLABORATOR_ADDED` | experience_album | 登记协作者 |
| `ENTRY_ADDED` | experience_album | 新增一条生活经验（亲历时间、推荐理由、适用条件、负面体验、证据附件摘要） |
| `ENTRY_UPDATED` | experience_album | 作者/协作者更新判断；显式合并时带 `merged_from_event_ids` |
| `AUTHOR_WITHDREW` | experience_album | 作者撤回认可，原文保留 |
| `COMMENT_CITED` | experience_album | 评论引用条目，固化引用时的条目版本 |
| `SNAPSHOT_SHARED` | shared_snapshot | 不可变分享快照，含原文与当时地点事实版本 |
| `PLACE_REGISTERED` | place_revision | 登记地点与事实（营业时间、无烟、母婴、寄存点等） |
| `PLACE_CHANGED` | place_revision | 地点事实变化（营业时间变化、设施撤除等） |
| `PLACE_MERGED` | place_revision | 地点合并，只迁移 `migrated_citations` 中被确认的引用 |
| `CORRECTION_SUBMITTED` | correction_case | 商家/读者提交事实更正 |
| `CORRECTION_DECIDED` | correction_case | 审核员裁决；审核员不得处理自己参与编辑的专辑 |

## 事件载荷

- `ENTRY_ADDED`：还需包含 `experience_at`, `audience_conditions`，可含 `negative_experience`、`identity_statement`、`attachments`。
- `ENTRY_UPDATED`：还需包含 `entry_id`, `place_id`, `author_id`, `experience_at`, `recommendation`, `audience_conditions`, `endorsed`。
- `SNAPSHOT_SHARED`：还需包含 `snapshot_hash`, `recipient_scope`, `entries`。
- `PLACE_CHANGED`：还需包含 `place_version`, `changed_facts`。
- `CORRECTION_SUBMITTED`：还需包含 `case_id`, `place_id`, `submitter_id`, `submitter_role`, `fact_patch`。

## 上层业务规则（src/experience_album_freshness/）

交换层只负责稳定报告结构、枚举、时间、版本和必需载荷问题；下列规则由业务服务承担：

1. **请求幂等**：`request_id` 相同的编辑请求只执行一次，重试返回首次结果。
2. **同版本异内容冻结**：同一聚合同一版本号出现异内容事件时，对应条目冻结，拒绝后续修改。
3. **显式合并**：协作者并发改同一条目触发冻结后，必须带着冲突事件标识提交显式合并，不能静默覆盖。
4. **快照不可变**：作者可更新判断，但已分享快照保留原文；阅读快照时标注失效/变化的地点。
5. **地点合并**：只迁移被确认的引用；针对旧门店的亲身体验保留在旧地点下，不被吞掉。
6. **事实更正边界**：商家可提交地点事实更正，不能删除或修改用户观点；裁决通过后只产生 `PLACE_CHANGED`。
7. **审核回避**：审核员不得裁决自己参与编辑（owner/协作者/条目编辑）过的专辑相关案件。
8. **附件最小可见**：附件中的个人信息只对作者本人、上传者、专辑 owner 与协作者可见，其他角色看到脱敏摘要。
9. **定时新鲜度检查**：使用可注入时钟；通知键持久化，重启后不重复通知。
