# 仁谱 Renpu

> **人脉的谱系。** 把你的联系人变成一张图谱 —— 谁是谁的人、在哪些群共现、
> 什么时候答应过什么、上次聊到哪里。任何 agent 都能查、能写。

```
renpu who 林夏        # 一个人的完整档案：关系 + 群 + 承诺 + 时间线
renpu edges             # 关系谱：谁跟谁在多少个群里共现
renpu people --days 7   # 最近还有来往的人
```

---

## 这东西解决什么

你有过这种时刻吗——

> 「我跟 XXX 什么关系来着？」

然后你翻了半小时聊天记录，或者干脆没想起来。

仁谱把你微信里的联系人变成**结构化的关系资产**：谁和谁在同一个群、谁答应过你什么、
上次聊到哪里、哪个人最近冷了。这些答案应该是**一次查询**，不是一次考古。

---

## 和「微信日报工具」的区别（重要，先看这段）

市面上已经有做得不错的同类项目（比如做每日情报日报的思路），它们回答的是：

> 「**有什么该做**」—— 今天有哪些消息没回、有哪些商机、今天要处理哪 10 件事

仁谱回答的是另一个问题：

> 「**他是谁的人**」—— 这个人在我的关系网里是什么位置、跟谁是一伙的、
> 我们的关系从哪一步开始变的、我欠他什么

| | 仁谱 Renpu | 情报日报类工具 |
|---|---|---|
| 核心对象 | **人（关系）** | 消息（情报） |
| 主输出 | 关系谱 + 事件 + 待办 | 日报 HTML + 商机清单 |
| 查询「X 是谁」 | ✅ 档案 + 群 + 关系边 | ⚠️ 只能捞出 X 说过的话 |
| 关系推理 | ✅ **群共现建图**（谁跟谁一起出现过） | ❌ 无 |
| 跨渠道统一人实体 | ✅ 已预留（同一人多渠道合并） | ❌ 按会话切片 |
| 状态 | 早期 v0.1 | 部分已商业化 |

**一句话**：日报工具帮你处理今天的事，仁谱帮你记住你这个人。
两者不冲突，可以并存。

---

## 5 分钟上手

```bash
# 1. 装依赖（唯一必需项）
pip install pycryptodome

# 2. 体检 + 建库 + 零配置体验
renpu doctor && renpu init && renpu ingest --demo

# 3. 随便玩
renpu who 林夏          # 一人档案
renpu edges               # 关系谱
renpu search 黑客松        # 全文检索
renpu feed --days 3       # 近期消息流
renpu serve --port 8877   # 起本地 API，任何 agent 都能调
```

> 详细：[QUICKSTART.md](QUICKSTART.md) · 接真微信：[INSTALL.md](INSTALL.md) · 卡住了：[TROUBLESHOOT.md](TROUBLESHOOT.md)

---

## 它是什么 / 不是

**是**：本地优先、渠道无关、agent 可调用的关系数据中枢。微信是第一个 adapter，不止微信。

**不是**：
- 不是抽取脚本（不是「抽出→存 md」的旧模式）
- 不是云服务（库全程在本机，不上传）
- 不是关系分析 AI（结论由你和你的 agent 写回 `event`/`todo` 表，程序不替你下判断）
- 不是 CRM（它不替你维护关系，只让你能问、能写）

---

## 架构

```
   微信 4.x 本地库（只读，绝不写）
        │  内置解密器（SQLCipher4 + WAL merge）
        ▼
   fresh_<YYYY-MM-DD>/{contact, message_0, session, contact_fts}.db
        │  adapters/wechat.py
        ▼
 ┌──────────────────────────────┐
 │  renpu.db（SQLite WAL）       │  ← 唯一真源
 │  person / identity           │     人（跨渠道统一实体）
 │  tag / person_tag            │     标签（微信标签 + 你自己打的）
 │  room / room_member          │     群与成员 ← 关系推理的原料
 │  conversation                │     会话
 │  msg_index / msg_fts         │     消息索引（FTS 可检索 + PII 已脱敏）
 │  event / edge / todo         │     关系事件 / 关系谱边 / 待办
 └──────────────────────────────┘
        │                          │
   renpu（人/终端）           renpu serve（agent）
   renpu who 姓名              GET /api/who?name=...
   renpu edges                 GET /api/edges
   renpu note "..."            POST /api/note
```

**关系谱（edge）怎么来的**：两个联系人同时出现在 N 个群里 → 建立一条权重 N 的边。
不需要任何人手动录入，摄取时自动算出来。

---

## 数据落在哪

跨平台默认（什么都不用配）：

| 系统 | 数据库 | 配置文件 |
|---|---|---|
| Windows | `%LOCALAPPDATA%\renpu\renpu.db` | `%LOCALAPPDATA%\renpu\config.json` |
| macOS | `~/Library/Application Support/renpu/renpu.db` | 同目录 `config.json` |
| Linux | `~/.local/share/renpu/renpu.db` | `~/.config/renpu/config.json` |

想放网盘或工作区？设 `RENPU_DATA_HOME=~/vault/people`。

---

## 两个数据源

| Source | 需要配置吗 | 说明 |
|---|---|---|
| **demo** | ❌ 不用 | 合成数据（10 人 / 2000 消息 / 33 条关系边）。先跑这个验证产品合不合适。 |
| **wechat** | 配 `wc_live` 路径 | 你自己的微信 4.x 库。只读，摄取 30-90 秒。 |

> **demo 数据里所有姓名都是虚构的**（林夏 / 苏见白 / 陈屿 / 周斯年 …），
> wxid 一律是 `wx:demo_*` 前缀，关系定性也全是通用标签。
> 它不包含任何真实联系人——这是公开发布的硬要求，不是可选项。

---

## 命令全表

```text
renpu doctor                      环境自检（跑不起来先看这个）
renpu init                        建库
renpu ingest [--demo] [--days 90] 摄取；--demo 零配置；--pull 先解密再摄取
renpu stats                       总览
renpu people [-q 词] [--days N]   人列表
renpu who <姓名>                   单人档案：档案+近期对话+群+关系谱+待办+事件
renpu edges [--limit N]           关系谱（群共现强度）
renpu search <关键词>              全文检索
renpu feed [--days N]             近期消息流
renpu rooms [--limit N]           群列表
renpu todo                        待办清单
renpu note "内容" [--name 姓名] [--kind promise] [--todo]
renpu todo --done <id>            关待办
renpu serve [--port 8877]         起本地 HTTP API（仅 127.0.0.1）
renpu --json <cmd>                结构化输出（给程序用）
renpu --verbose <cmd>             出错时打印完整 traceback
```

---

## 硬纪律（代码里就守着，不是建议）

1. **PII 红线**：库在用户目录，不入 git、不上云、不进公开线。入库自动脱敏手机号/身份证/卡号。
2. **只读源库**：全程零写入你的微信数据。解密快照落在仁谱自己的目录里。
3. **判断不臆测**：`person.tier/circle/org` 这些「他是什么人」的字段，程序不自动填——那是你的判断位。
4. **缺口不吞**：语音（加密字节）、撤回（不可复原）如实说「未读到」，不假装读懂了。
5. **API 只绑本机**：`127.0.0.1`，不监听 `0.0.0.0`。要跨机自己走隧道，那是显式动作不是默认。

---

## 已知边界

| 项 | 状态 |
|---|---|
| 语音消息 | 加密字节，未转写 = 缺口 |
| 撤回消息 | 不可复原 = 缺口 |
| 图片/视频/文件 | 只存类型不存内容 |
| 精确加好友时间 | 源库无此字段，不做推断 |
| 朋友圈（sns） | 未接（可作下个 adapter） |
| PC 未同步的消息 | 库里没有 ≠ 对方没发 |

---

## 接下一个数据源

新建 `adapters/<channel>.py`，实现 `ingest()` 调 `core.py` 的六个域函数即可，
CLI 和 API 一行都不用改。字段已预留：`identity(channel, channel_id)` 一人多渠道、
`person.person_key` 跨渠道稳定 ID。

## 许可

Apache-2.0 · 蒋平 Jacky Jiang