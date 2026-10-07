# 生活经验专辑时效台

保存生活经验专辑的地点版本、分享快照和事实纠错，让过期变化可见但不抹去原判断。

## 目录

- `contracts/domain.schema.json`：事件信封、对象类型和事件载荷约定。
- `data/sample.json`：可直接校验的中文联调样例。
- `src/experience_album_freshness/`
  - `contracts.py`：交换层校验（结构/枚举/时区/版本/必需载荷）。
  - `store.py`：事件存储——请求幂等、同版本异内容冻结、条目级乐观锁、JSON 持久化。
  - `model.py`：事件折叠出的读模型（专辑、条目、地点版本、快照、评论、纠错申诉）。
  - `access.py`：角色身份声明与附件个人信息遮蔽。
  - `service.py`：`AlbumService`——条目协作编辑、快照、地点合并、商家更正、审核回避、阅读视图。
  - `freshness.py`：`FreshnessChecker`——注入时钟的定时检查与持久化通知去重台账。
  - `clock.py`：`SystemClock` / `FixedClock`。
  - `errors.py`：稳定错误码（`version_frozen`、`merge_required`、`reviewer_conflict` 等）。
- `tests/`：契约、存储、服务、新鲜度检查共 40+ 条用例。
- `docs/domain.md`：领域对象、事件语义与业务规则。

## 快速使用

```python
from datetime import datetime, timezone
from experience_album_freshness import AlbumService, EventStore, FixedClock, Identity, Role

clock = FixedClock(datetime(2026, 10, 1, tzinfo=timezone.utc))
svc = AlbumService(EventStore(), clock)
author = Identity.of("u-author", Role.AUTHOR)

svc.create_album(author, "album-1", request_id="req-1")
svc.register_place(author, "album-1", "p1", name="清风无烟餐厅",
                   facts={"smoke_free": True, "hours": "10:00-22:00"}, request_id="req-2")
svc.add_entry(author, "album-1", "e1", "p1",
              title="带娃午餐", experience_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
              audience_conditions="0-3 岁婴幼儿家庭", request_id="req-3")
```

所有写命令都必须带 `request_id`（相同请求只执行一次）；条目更新带
`expected_version`，并发冲突时按 `MergeRequiredError` 中返回的当前状态走显式合并。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 样例校验

```bash
PYTHONPATH=src python3 -m experience_album_freshness.cli contracts/domain.schema.json data/sample.json
```

命令成功时输出 `valid`；校验失败时逐行输出字段、代码和中文说明，并以非零状态结束。
