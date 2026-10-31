-- QQ 陪伴机器人 —— 数据库结构
-- SQLite 3.35+ （FTS5 必须编译进去，Python 内置通常已包含）
-- 所有时间字段统一用 Unix 秒（INTEGER），避免时区问题

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- 用户（内部 ID 与 openid 分离）
-- ---------------------------------------------------------------------------
-- 为什么不用 openid 当主键：不同 bot(AppID) 拿到的 openid 不同，
-- 将来换 bot 或加 bot 时，同一个人会变成两个账号，记忆全丢。
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    INTEGER NOT NULL,
    -- 状态：'new' 未绑定 / 'active' 可用 / 'blocked' 手动封禁
    status        TEXT    NOT NULL DEFAULT 'new'
);

CREATE TABLE IF NOT EXISTS identities (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    appid         TEXT    NOT NULL,
    openid        TEXT    NOT NULL,
    created_at    INTEGER NOT NULL,
    UNIQUE (appid, openid)
);
CREATE INDEX IF NOT EXISTS idx_identities_user ON identities(user_id);

-- ---------------------------------------------------------------------------
-- 客户配置：key（加密）+ 模型 + 人格
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS configs (
    user_id          INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    -- Fernet 密文（base64）。明文永不落库。
    key_cipher       TEXT,
    -- sha256(key) 前 8 位，用于日志与去重，不可逆
    key_fingerprint  TEXT,
    -- 末 4 位，仅用于展示 sk-****abcd
    key_suffix       TEXT,
    -- 绑定的中转站 base，例如 https://relay.example.com/v1
    api_base         TEXT,
    model            TEXT,
    -- 生图 key：和聊天 key 分开配。
    -- 为什么分开：聊天和生图常常不在同一个中转站/分组，
    -- 而且生图贵得多，客户可能想用不同的额度。
    -- 没配 image_cipher 时，客户要图就明确告知"没配生图 key"。
    image_cipher     TEXT,
    image_suffix     TEXT,
    image_api_base   TEXT,
    image_model      TEXT,
    -- 该 key 实际可用的模型（JSON 数组），来自 /v1/models，随校验一起刷新。
    -- 有这个缓存，客户再发 /model 时不用重新请求中转站。
    available_models TEXT,
    persona_id       TEXT    NOT NULL DEFAULT 'default',
    -- 语气预设：normal/gentle/sharp/quiet/playful/clingy
    modifier_id      TEXT    NOT NULL DEFAULT 'normal',
    -- 旧字段：单个人设时期的文本。已迁移到 personas 表，仅保留兼容。
    custom_persona   TEXT,
    -- 当前激活的人设 id（NULL = 用默认陪聊风格）
    active_persona_id INTEGER,
    -- 校验缓存：最后一次成功校验的时间与结果
    key_checked_at   INTEGER NOT NULL DEFAULT 0,
    key_valid        INTEGER NOT NULL DEFAULT 0,   -- 0/1
    key_balance      INTEGER NOT NULL DEFAULT 0,   -- quota 单位
    key_unlimited    INTEGER NOT NULL DEFAULT 0,   -- 0/1
    -- 最近一次校验失败原因，用于提示话术
    key_last_error   TEXT,
    updated_at       INTEGER NOT NULL
);

-- ---------------------------------------------------------------------------
-- 消息归档：永不失忆的唯一保证
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    persona_id  TEXT    NOT NULL,
    role        TEXT    NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT    NOT NULL,
    -- 预切好的 bigram 索引，空白分隔
    bigrams     TEXT    NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_user_time ON messages(user_id, created_at);

-- 消息的向量（语义检索用）。
-- 单独一张表而不是加列，原因：
--   ① 向量是 BLOB，跟 messages 放一起会让主表扫描变慢
--   ② 没生成向量的消息不该占位（NULL 也可，但分表更好删）
--   ③ 换 embedding 模型时只需清空这张表重算
-- vec 是 int8 量化后的 BLOB：1024 维 × 1 字节 = 1KB/条
-- 磁盘 10 万条约 100MB，内存只加载当前用户的（5000 条 = 5MB）
CREATE TABLE IF NOT EXISTS message_vectors (
    message_id  INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL,
    dim         INTEGER NOT NULL,
    vec         BLOB    NOT NULL,
    model       TEXT    NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mv_user ON message_vectors(user_id);
-- 查询是 WHERE user_id=? AND persona_id=? ORDER BY id DESC，
-- 索引必须把 id 放在最后才能免掉 TEMP B-TREE 排序。
CREATE INDEX IF NOT EXISTS idx_messages_user_persona ON messages(user_id, persona_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_user_time ON messages(user_id, created_at);

-- 消息的向量（语义检索用）。
-- 单独一张表而不是加列，原因：
--   ① 向量是 BLOB，跟 messages 放一起会让主表扫描变慢
--   ② 没生成向量的消息不该占位（NULL 也可，但分表更好删）
--   ③ 换 embedding 模型时只需清空这张表重算
-- vec 是 int8 量化后的 BLOB：1024 维 × 1 字节 = 1KB/条
-- 磁盘 10 万条约 100MB，内存只加载当前用户的（5000 条 = 5MB）
CREATE TABLE IF NOT EXISTS message_vectors (
    message_id  INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL,
    dim         INTEGER NOT NULL,
    vec         BLOB    NOT NULL,
    model       TEXT    NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mv_user ON message_vectors(user_id);

-- 外部内容 FTS5：不重复存正文，省一半空间
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    bigrams,
    content='messages',
    content_rowid='id',
    tokenize='unicode61'
);

-- 让 messages 的增删自动同步到 FTS
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content, bigrams)
    VALUES (new.id, new.content, new.bigrams);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, bigrams)
    VALUES ('delete', old.id, old.content, old.bigrams);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, bigrams)
    VALUES ('delete', old.id, old.content, old.bigrams);
    INSERT INTO messages_fts(rowid, content, bigrams)
    VALUES (new.id, new.content, new.bigrams);
END;

-- ---------------------------------------------------------------------------
-- 滚动摘要（可回滚）
-- ---------------------------------------------------------------------------
-- anchor_message_id 对应"生成时的最后一条消息"：
-- 如果那条消息被删除，摘要失效，回滚到上一条有效摘要。
-- 客户自建的人设，可以存多个，随时切换。
-- 每个人设对应一条独立对话线（messages.persona_id = 'p<id>'），
-- 但事实卡（facts）是共享的 —— 换个角色也该记得你是谁。
CREATE TABLE IF NOT EXISTS personas (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_personas_user ON personas(user_id);

CREATE TABLE IF NOT EXISTS summaries (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    persona_id         TEXT    NOT NULL,
    content            TEXT    NOT NULL,
    anchor_message_id  INTEGER NOT NULL,
    covered_until      INTEGER NOT NULL,
    created_at         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summaries_user ON summaries(user_id, persona_id, id DESC);

-- ---------------------------------------------------------------------------
-- 结构化事实卡
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS facts (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    persona_id  TEXT    NOT NULL DEFAULT '',   -- '' = 跨人格共享
    key         TEXT    NOT NULL,
    value       TEXT    NOT NULL,
    confidence  REAL    NOT NULL DEFAULT 0.8,
    -- 来源：auto = 模型自动抽取，manual = 客户自己写的。
    -- manual 的不会被自动抽取覆盖，也不会被 /facts clear 之外的任何操作删掉。
    source      TEXT    NOT NULL DEFAULT 'auto',
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (user_id, persona_id, key)
);

-- ---------------------------------------------------------------------------
-- 网页绑定码（一次性）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bind_codes (
    code        TEXT    PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    used_at     INTEGER,
    -- 'chat' = 聊天 key，'image' = 生图 key。
    -- 两者必须分开：同一个人可能用不同中转站/分组，
    -- 而且生图 key 泄露的代价更高（按张计费）。
    kind        TEXT    NOT NULL DEFAULT 'chat'
);

-- ---------------------------------------------------------------------------
-- 用量统计（按天聚合，只记不用于计费）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS usage_daily (
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day           TEXT    NOT NULL,             -- 'YYYY-MM-DD'（Asia/Shanghai）
    turns         INTEGER NOT NULL DEFAULT 0,
    tokens_in     INTEGER NOT NULL DEFAULT 0,
    tokens_out    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);

-- ---------------------------------------------------------------------------
-- 异步任务队列（摘要 / 事实抽取）
-- ---------------------------------------------------------------------------
-- 落库而不是纯内存，防止重启丢任务。
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL,               -- 'summary' / 'facts'
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    payload     TEXT    NOT NULL DEFAULT '{}',
    status      TEXT    NOT NULL DEFAULT 'pending',  -- pending/running/done/failed
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id);

-- ---------------------------------------------------------------------------
-- 元信息（schema 版本等）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta (
    k  TEXT PRIMARY KEY,
    v  TEXT NOT NULL
);
INSERT OR IGNORE INTO meta (k, v) VALUES ('schema_version', '1');
