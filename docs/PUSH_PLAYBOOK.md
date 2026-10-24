# 仁谱 Renpu · GitHub 推送指令包

> 状态：**已过闸，等待授权执行**。本文件不代你 push。
> 生成时间：2026-10-22

---

## 一 · 推送前 PII 终检（对将要推送的分支，不是源目录）

| 闸 | 检查项 | 结果 |
|---|---|---|
| 内部节点名 | 26 个文件全文扫描 | **0 命中 ✅** |
| 真实人名 | 26 个文件全文扫描 | **0 命中 ✅** |
| 本机用户名 | 26 个文件全文扫描 | **0 命中 ✅** |
| 误替换产物 | 26 个文件全文扫描 | **0 命中 ✅** |

**结论：✅ 可发布**

- 待推分支：`public-release`（单 commit `4373519`）
- commit 数：**1**（原始 3 个含污染 commit 已压成 1 个）

---

## 二 · 仓元数据建议

| 字段 | 建议值 | 理由 |
|---|---|---|
| 仓名 | `renpu` | 短、好念、与产品名一致。`renpu-genealogy` 更描述但太长。 |
| 可见性 | **public** | 开源项目，公开才有 star / PR / 反馈 |
| License | **AGPL-3.0-only** | 换证（2026-10-22）：防「fork 改个名闭源卖」+ 网络服务须开源。闭源需求走商业授权。 |
| Description | `Local-first relationship genealogy: turn WeChat records into a queryable graph of people. CLI + 127.0.0.1-only API for agents.` | 一句话讲清「人」不是「消息」 |

**Topics**（GitHub 最多 20 个，建议 8 个）：

```
wechat  relationship-graph  social-graph  personal-vault
local-first  cli  agent-tools  contact-management
```

**刻意不加**：`intelligence`、`wechat-intelligence`、`hub` —— 会与 wechat-intelligence-hub 的搜索结果混淆。

---

## 三 · 执行命令（复制即用 · 需蒋平授权）

```bash
cd /c/research_workspace/_hub/renpu

# 1. 在 GitHub 上手动建空仓（不要勾选 README / License / .gitignore）
#    https://github.com/new  →  仓名填 renpu

# 2. 关联远端
git remote add origin git@github.com:jackyjinggit/renpu.git

# 3. 推干净分支（不是 master！）
git push -u origin public-release:main

# 4. 回本地主干（master 含污染历史，勿推）
git checkout master
git branch -D public-release   # 推完可删，本地已有 tag 归档
```

### 🔴 关键提醒

**只能推 `public-release`，绝对不要推 `master`。**

`master` 上三个 commit 的历史里有真实联系人姓名（`archive/*` tag 同样指向那些 commit，
**不要 `git push --tags`**）。推上去即永久公开，无法撤回。

---

## 四 · 推后动作

```bash
# 打首个 tag（干净历史上的）
git checkout public-release
git tag -a v0.3.0 -m "Renpu v0.3.0 -- first public release"
git push origin v0.3.0

# 上传 zip 作为 Release 附件（可选，别人可免 clone 直接下载）
# 在 GitHub Release 页面拖入 renpu-v0.3.zip
```

**建议开 Release**：内测阶段给人 zip 比 clone 方便，且 zip 是 64 KB 单文件。

---

## 五 · 回执清单（推完填，回写当日日志）

| 项 | 值 |
|---|---|
| 仓库 URL | `https://github.com/jackyjinggit/renpu` |
| 推送分支 | `public-release:main` |
| 首 commit | 待填 |
| 执行时间 | 待填 |
| Release | 待填（是否开 v0.3.0） |
| zip 附件 | 待填 |
| 执行人 | 蒋平授权 / 鸿窗执行 |

---

## 六 · 录视频联动（重要）

录屏时的示例人物**一律用 demo 虚构名「林夏」**，不要用旧代号。
视频一旦上传到 B站/微博/知乎 也是公开资产，撤回比删 commit 更麻烦。
