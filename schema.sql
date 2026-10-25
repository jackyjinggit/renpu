-- 仁谱 Renpu 统一 schema · v0.2 · 2026-10-22
-- 设计原则：
--   1. 渠道无关（channel-agnostic）：微信只是第一个 source，后接飞书/邮件不改表结构
--   2. 人为主键（person_key），平台 ID 只作 attribute → 换平台不丢人
--   3. 消息不整体入库，只存「可检索索引 + 近窗口原文」，正文留在解密库按需拉
--   4. PII 红线区：DB 落在用户自定的私密目录（默认在用户主目录下的独立数据目录），不出本机

PRAGMA journal_mode=WAL;

-- ─────────────── 人（跨渠道统一实体） ───────────────
CREATE TABLE IF NOT EXISTS person (
  person_key   TEXT PRIMARY KEY,        -- 稳定 ID：wx:<wxid> / fs:<open_id> / md5(name+tag)
  display_name TEXT NOT NULL,           -- 展示名（remark > nick > alias）
  real_name    TEXT,                    -- 真名/曾用名
  tier         TEXT,                    -- 人脉分层：AAAA/AAA/Aa/a/未分层
  circle       TEXT,                    -- 圈层：家人/发小/同学/同事/行业/线上/客户
  org          TEXT,                    -- 所属组织
  role         TEXT,                    -- 角色/头衔
  relation     TEXT,                    -- 与蒋平的关系描述
  avatar       TEXT,
  first_seen   INTEGER,                -- 首次出现 unix
  last_active  INTEGER,                -- 最近活跃 unix
  msg_total    INTEGER DEFAULT 0,
  msg_d30      INTEGER DEFAULT 0,
  msg_d7       INTEGER DEFAULT 0,
  wechat_type  INTEGER,                -- local_type：3 好友 / 2 群 / 1 公众号 …
  source       TEXT DEFAULT 'wechat',
  is_room      INTEGER DEFAULT 0,
  note         TEXT,
  created_at   INTEGER,
  updated_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_person_name  ON person(display_name);
CREATE INDEX IF NOT EXISTS idx_person_tier  ON person(tier);
CREATE INDEX IF NOT EXISTS idx_person_active ON person(last_active DESC);

-- ─────────────── 平台身份（一人多渠道） ───────────────
CREATE TABLE IF NOT EXISTS identity (
  person_key  TEXT NOT NULL,
  channel     TEXT NOT NULL,           -- wechat / feishu / mail / phone
  channel_id  TEXT NOT NULL,           -- wxid / open_id / email
  alias       TEXT,                    -- 微信号
  nickname    TEXT,
  remark      TEXT,
  signature   TEXT,                    -- 个性签名/介绍
  extra       TEXT,                    -- JSON 扩展
  PRIMARY KEY (channel, channel_id)
);
CREATE INDEX IF NOT EXISTS idx_ident_pk ON identity(person_key);

-- ─────────────── 标签（微信标签 / 画像群分层） ───────────────
CREATE TABLE IF NOT EXISTS tag (
  tag_id    TEXT PRIMARY KEY,
  name      TEXT NOT NULL,
  source    TEXT,                      -- wechat_label / manual / ai
  parent    TEXT,
  created_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tag_name ON tag(name);

CREATE TABLE IF NOT EXISTS person_tag (
  person_key TEXT NOT NULL,
  tag_id     TEXT NOT NULL,
  source     TEXT,
  PRIMARY KEY (person_key, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_pt_tag ON person_tag(tag_id);

-- ─────────────── 群（房间） ───────────────
CREATE TABLE IF NOT EXISTS room (
  room_key   TEXT PRIMARY KEY,
  name       TEXT,
  channel_id TEXT,
  owner      TEXT,
  member_cnt INTEGER DEFAULT 0,
  msg_total  INTEGER DEFAULT 0,
  msg_d30    INTEGER DEFAULT 0,
  last_active INTEGER,
  announcement TEXT,
  source     TEXT DEFAULT 'wechat'
);
CREATE TABLE IF NOT EXISTS room_member (
  room_key TEXT NOT NULL,
  person_key TEXT NOT NULL,
  PRIMARY KEY (room_key, person_key)
);

-- ─────────────── 会话（人 ↔ 人，或 人 ↔ 群） ───────────────
CREATE TABLE IF NOT EXISTS conversation (
  conv_key   TEXT PRIMARY KEY,
  person_key TEXT,                     -- 群会话为空
  room_key   TEXT,
  title      TEXT,
  msg_total  INTEGER DEFAULT 0,
  last_ts    INTEGER,
  last_msg   TEXT,
  last_from_me INTEGER,
  updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_conv_last ON conversation(last_ts DESC);

-- ─────────────── 消息索引（检索用，不存全量原文） ───────────────
CREATE TABLE IF NOT EXISTS msg_index (
  msg_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  conv_key   TEXT NOT NULL,
  ts         INTEGER NOT NULL,
  sender     TEXT,                     -- person_key（自己=me）
  dir        INTEGER,                  -- 1 我发 / 0 收到
  mtype      INTEGER,                  -- local_type
  snippet    TEXT,                     -- ≤200 字脱敏片段
  has_pii    INTEGER DEFAULT 0,
  source     TEXT DEFAULT 'wechat'
);
CREATE INDEX IF NOT EXISTS idx_msg_conv_ts ON msg_index(conv_key, ts DESC);
CREATE INDEX IF NOT EXISTS idx_msg_ts     ON msg_index(ts DESC);
-- 幂等键：同一会话同秒同内容同类型只留一条 → 重跑 sync 不膨胀
CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_dedup ON msg_index(conv_key, ts, snippet, mtype);

-- ─────────────── 人际事件（关系状态变化的唯一真源） ───────────────
CREATE TABLE IF NOT EXISTS event (
  event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  person_key TEXT,
  kind       TEXT NOT NULL,            -- first_talk / recontact / gift / redpacket /
                                       -- meet / intro / promise / silence / breach / ai_note
  ts         INTEGER NOT NULL,
  title      TEXT,
  detail     TEXT,
  evidence   TEXT,                     -- 原话/消息 ID 引用（可溯源）
  source     TEXT DEFAULT 'ai',
  created_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_event_person ON event(person_key, ts DESC);
CREATE INDEX IF NOT EXISTS idx_event_kind   ON event(kind, ts DESC);

-- ─────────────── 关系边（人 ↔ 人，含群共现推断） ───────────────
CREATE TABLE IF NOT EXISTS edge (
  src     TEXT NOT NULL,
  dst     TEXT NOT NULL,
  kind    TEXT,                        -- co_group / intro / explicit / ai_infer
  weight  REAL DEFAULT 1.0,
  rooms   TEXT,                        -- 共现群 room_key 列表（JSON）
  note    TEXT,
  updated_at INTEGER,
  PRIMARY KEY (src, dst, kind)
);

-- ─────────────── 待办（回访/跟进/承诺兑现） ───────────────
CREATE TABLE IF NOT EXISTS todo (
  todo_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  person_key TEXT,
  kind      TEXT,                      -- reply / followup / promise / gift / birthday
  title     TEXT NOT NULL,
  due       INTEGER,
  status    TEXT DEFAULT 'open',       -- open / done / dropped
  evidence  TEXT,
  created_at INTEGER,
  closed_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_todo_open ON todo(status, due);

-- ─────────────── 摄取审计（每次 ingest 留痕，可回溯） ───────────────
CREATE TABLE IF NOT EXISTS sync_log (
  sync_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  source    TEXT NOT NULL,
  src_path  TEXT,
  started_at INTEGER,
  ended_at  INTEGER,
  rows_people INTEGER DEFAULT 0,
  rows_msgs   INTEGER DEFAULT 0,
  rows_events INTEGER DEFAULT 0,
  note      TEXT
);

-- ─────────────── 全文检索（独立 fts5 表，snippet 独立存一份换检索简单可靠） ───────────────
CREATE VIRTUAL TABLE IF NOT EXISTS msg_fts USING fts5(
  snippet, person_key UNINDEXED, conv_key UNINDEXED, ts UNINDEXED
);