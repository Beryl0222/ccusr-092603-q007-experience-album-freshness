# 领域约定

保存生活经验专辑的地点版本、分享快照和事实纠错，让过期变化可见但不抹去原判断。

聚合对象包括`experience_album`、`place_revision`、`shared_snapshot`、`correction_case`。事件类型包括`ENTRY_ADDED`、`SNAPSHOT_SHARED`、`PLACE_CHANGED`、`CORRECTION_DECIDED`、`AUTHOR_WITHDREW`。所有时间都必须携带时区，版本号从 1 开始递增，校验层不会替调用方改写输入。

## 事件载荷

- `ENTRY_ADDED`：还需包含 `experience_at`, `audience_conditions`。
- `SNAPSHOT_SHARED`：还需包含 `snapshot_hash`, `recipient_scope`。
- `PLACE_CHANGED`：还需包含 `place_version`, `changed_facts`。

同一事件标识的幂等与冲突处理属于上层业务服务职责；交换层只负责稳定报告结构、枚举、时间、版本和必需载荷问题。
