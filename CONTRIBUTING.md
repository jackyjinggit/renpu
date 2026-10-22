# Contributing to 仁谱 Renpu

感谢参与。这里是几条硬约束，先读再提 PR。

## 硬约束（违反会被要求改）

1. **PII 绝不进仓**
   - 任何真实 `*.db`、`db_key.txt`、`config.json` 一律 `.gitignore` 挡住
   - 测试数据一律用 `adapters/demo.py` 的合成数据
   - 提交前跑 `git status`，看到 `*.db` 就是红线

2. **不做云端**
   - 任何新功能默认本地跑；引网络依赖必须在 PR 里说明为什么
   - 人际数据是敏感资产，出本机 = 需要显式讨论，不是默认行为

3. **源库只读**
   - 任何 adapter 都不得写入上游数据源（微信库、飞书库等）
   - 解密产物必须落在 仁谱 自己的目录下

4. **判断不臆测**
   - `person.tier/circle/org` 等他人画像字段，程序不得自动填
   - `event` 表的每条结论必须能溯源（带 `evidence`）

## 加新数据源（adapter）

不用改上层任何代码：

1. 新建 `adapters/<channel>.py`
2. 实现 `ingest(days=..., progress=...) -> dict`
3. 只调 `core.py` 的六域函数：`upsert_person / upsert_identity / tag_person / upsert_room / add_room_member / upsert_conversation / add_message / add_event / add_edge / add_todo`
4. 在 `cli.py` 的 `cmd_ingest` 加一个分支
5. 在 `plugin.json` 的 `channels` 里加一条声明
6. 在 `tests/test_smoke.py` 加一条冒烟

**跨渠道唯一标识**：`identity(channel, channel_id)` 表已支持一人多渠道，`person.person_key` 是稳定 ID。

## 加新命令

`cli.py` 加 `cmd_xxx(a)` + `sub.add_parser(...)`，输出走 `out(obj, a.json)`：
- `--json` 给 agent（结构化）
- 默认给人看（`_pretty` 渲染）
- 错误抛 `SystemExit("人话 + 修复指引")`，别抛裸 traceback

## 本地跑测试

```bash
pip install pycryptodome
python tests/test_smoke.py        # 应 ALL GREEN
```

## 提交信息

- 第一人称，中文或英文都行，说清"改了什么"和"为什么"
- 一个 PR 只做一件事

## License

贡献即同意本项目 Apache-2.0。