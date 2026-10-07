# 生活经验专辑时效台

保存生活经验专辑的地点版本、分享快照和事实纠错，让过期变化可见但不抹去原判断。

## 目录

- `contracts/domain.schema.json`：事件信封、对象类型和事件载荷约定。
- `data/sample.json`：可直接校验的中文联调样例。
- `src/experience_album_freshness/`：
  - `contracts.py`：交换层契约校验（枚举、时区、版本、必备载荷）。
  - `storage.py`：事件溯源存储，JSON 文件原子落盘；承载请求幂等索引与通知去重键。
  - `model.py`：事件回放（专辑/条目、地点版本与历史事实、纠错案件、分享快照、评论引用）。
  - `service.py`：时效台业务规则（见下）与阅读视图。
  - `permissions.py`：附件个人信息按角色脱敏。
  - `freshness.py`：可注入时钟的定时新鲜度检查。
  - `clock.py`：`SystemClock` / `FixedClock`。
  - `errors.py`：业务冲突类型。
- `tests/`：契约与全部业务规则测试。
- `docs/domain.md`：领域对象、事件语义与业务规则。

## 业务规则

- 相同 `request_id` 的编辑请求只执行一次，重试返回首次结果，重启后仍幂等。
- 同一版本号出现异内容事件：条目冻结（`ENTRY_FROZEN`），必须逐支列出冲突事件做显式合并。
- 协作者并发改同一条目：乐观版本号 + 冻结 + 显式合并，禁止静默覆盖；同内容并发收敛。
- 作者可更新判断或撤回认可；已分享快照保留原文，并标注失效/合并地点、变化事实、作者是否仍认可。
- 地点合并只迁移被确认的引用；针对旧门店的体验保留在旧地点。
- 商家只能提交事实更正，不能删除或修改用户观点；裁决接受后仅产生 `PLACE_CHANGED`。
- 审核员不得处理自己参与编辑（owner/协作者/条目编辑）的专辑案件。
- 附件 PII 仅作者本人、上传者、专辑 owner 与协作者可见，其他角色与匿名者看到脱敏摘要。
- 定时检查使用可注入时钟；通知键持久化，重启后不重复通知。

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
