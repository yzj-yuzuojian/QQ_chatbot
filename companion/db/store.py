"""数据访问层。

唯一的 SQL 入口：任何检索都必须走这里，保证 user_id 过滤不会被漏掉。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any

from .. import text as T

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


def now() -> int:
    return int(time.time())


class Store:
    """SQLite 封装。

    使用单连接 + 显式事务。SQLite 的并发模型在单进程 asyncio 下够用：
    所有写操作都是短事务，读操作走 WAL 不会阻塞写。
    """

    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

        # busy_timeout 必须设。默认是 0 —— 一撞锁立刻抛
        # "database is locked"，多人同时说话就会冒 500。
        # 5 秒足够等一个短事务写完。
        self.conn.execute("PRAGMA busy_timeout = 5000")
        # WAL：读写不互相阻塞。20 个客户同时聊天靠它。
        self.conn.execute("PRAGMA journal_mode = WAL")
        # NORMAL 在 WAL 下已经够安全（断电最多丢最后一条事务），
        # FULL 每条都 fsync，聊天场景没必要付那个代价。
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute("PRAGMA foreign_keys = ON")

        self._init_schema()

    def _init_schema(self) -> None:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            self.conn.executescript(f.read())
        self.conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """给已有表补新增的列。

        `CREATE TABLE IF NOT EXISTS` 对已存在的表什么都不做，
        所以每次加字段都要在这里补一条 ALTER TABLE。
        写在代码里而不是靠手工执行，避免部署时漏掉。
        """
        want: dict[str, list[tuple[str, str]]] = {
            "configs": [
                ("available_models", "TEXT"),
                ("modifier_id", "TEXT NOT NULL DEFAULT 'normal'"),
                ("custom_persona", "TEXT"),
                ("active_persona_id", "INTEGER"),
                ("image_cipher", "TEXT"),
                ("image_suffix", "TEXT"),
                ("image_api_base", "TEXT"),
                ("image_model", "TEXT"),
            ],
        }

        # bind_codes 补 kind 列（区分聊天 key / 生图 key）
        try:
            bcols = {
                r["name"] for r in self.conn.execute("PRAGMA table_info(bind_codes)")
            }
            if "kind" not in bcols:
                self.conn.execute(
                    "ALTER TABLE bind_codes ADD COLUMN kind TEXT NOT NULL DEFAULT 'chat'"
                )
                self.conn.commit()
                print("[migrate] bind_codes.kind 已添加")
        except Exception as e:
            print(f"[migrate] bind_codes.kind 跳过: {e}")

        # facts 表补 source 列（走单独一段，因为不在 configs 里）
        try:
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(facts)")}
            if "source" not in cols:
                self.conn.execute(
                    "ALTER TABLE facts ADD COLUMN source TEXT NOT NULL DEFAULT 'auto'"
                )
                self.conn.commit()
                print("[migrate] facts.source 已添加")
        except Exception as e:
            print(f"[migrate] facts.source 跳过: {e}")

        # ① 先补列（否则下面的迁移会因列不存在而失败）
        for table, columns in want.items():
            cur = self.conn.execute(f"PRAGMA table_info({table})")
            have = {r["name"] for r in cur.fetchall()}
            for name, ddl in columns:
                if name not in have:
                    try:
                        self.conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"
                        )
                        print(f"[migrate] {table}.{name} 已添加")
                    except Exception as e:
                        print(f"[migrate] {table}.{name} 失败: {e}")
        self.conn.commit()

        # ①.5 重建索引：老版本 idx_messages_user_persona 按 created_at，
        # 而查询用 ORDER BY id，会走 TEMP B-TREE。数据量上去后变慢。
        try:
            self.conn.execute("DROP INDEX IF EXISTS idx_messages_user_persona")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_user_persona "
                "ON messages(user_id, persona_id, id)"
            )
            self.conn.commit()
        except Exception as e:
            print(f"[migrate] 索引重建跳过: {e}")

        # ② 人设迁移：单个 custom_persona → personas 表
        try:
            rows = self.conn.execute(
                "SELECT user_id, custom_persona FROM configs "
                "WHERE custom_persona IS NOT NULL AND custom_persona != '' "
                "  AND active_persona_id IS NULL"
            ).fetchall()
            for r in rows:
                ts = now()
                cur = self.conn.execute(
                    """INSERT INTO personas (user_id, name, content, created_at, updated_at)
                       VALUES (?,?,?,?,?)""",
                    (r["user_id"], "默认人设", r["custom_persona"], ts, ts),
                )
                self.conn.execute(
                    "UPDATE configs SET active_persona_id=? WHERE user_id=?",
                    (cur.lastrowid, r["user_id"]),
                )
            if rows:
                self.conn.commit()
                print(f"[migrate] 迁移 {len(rows)} 个旧人设到 personas 表")
        except Exception as e:
            print(f"[migrate] 人设迁移跳过: {e}")

        # 数据迁移：早期版本按 persona_id 分对话线（default / tsundere / ...），
        # 现在改成单一线程 "main"。旧数据要搬过来，否则客户的历史记忆读不到。
        for table in ("messages", "summaries"):
            try:
                n = self.conn.execute(
                    f"UPDATE {table} SET persona_id='main' "
                    f"WHERE persona_id != 'main' AND persona_id != ''"
                ).rowcount
                if n:
                    self.conn.commit()
                    print(f"[migrate] {table}: {n} 行 persona_id → main")
            except Exception:
                pass

        # 新建的表（不在 schema.sql 里的话）
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS export_codes (
                   code        TEXT PRIMARY KEY,
                   user_id     INTEGER NOT NULL,
                   created_at  INTEGER NOT NULL,
                   expires_at  INTEGER NOT NULL,
                   used_at     INTEGER
               )"""
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------
    # 用户与身份
    # ------------------------------------------------------------------

    def get_or_create_user(self, appid: str, openid: str) -> int:
        """按 (appid, openid) 找用户，没有就建。

        返回内部 user_id。这是所有业务逻辑的主键——
        openid 只用于入口识别，不参与任何业务判断。
        """
        cur = self.conn.execute(
            "SELECT user_id FROM identities WHERE appid=? AND openid=?",
            (appid, openid),
        )
        row = cur.fetchone()
        if row:
            return int(row["user_id"])

        ts = now()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO users (created_at, status) VALUES (?, 'new')", (ts,)
            )
            uid = int(cur.lastrowid)
            self.conn.execute(
                "INSERT INTO identities (user_id, appid, openid, created_at) VALUES (?,?,?,?)",
                (uid, appid, openid, ts),
            )
            self.conn.execute(
                "INSERT INTO configs (user_id, updated_at) VALUES (?,?)", (uid, ts)
            )
        return uid

    # ------------------------------------------------------------------
    # 客户配置
    # ------------------------------------------------------------------

    def get_config(self, user_id: int) -> dict[str, Any]:
        cur = self.conn.execute("SELECT * FROM configs WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if not row:
            return {}
        return dict(row)

    def update_config(self, user_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now()
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [user_id]
        with self.conn:
            self.conn.execute(f"UPDATE configs SET {cols} WHERE user_id=?", vals)

    def set_user_status(self, user_id: int, status: str) -> None:
        """更新用户状态。注意 status 在 users 表，不在 configs 表。"""
        with self.conn:
            self.conn.execute(
                "UPDATE users SET status=? WHERE id=?", (status, user_id)
            )

    def set_key(
        self,
        user_id: int,
        cipher: str,
        fingerprint: str,
        suffix: str,
        api_base: str,
    ) -> None:
        self.update_config(
            user_id,
            key_cipher=cipher,
            key_fingerprint=fingerprint,
            key_suffix=suffix,
            api_base=api_base,
            key_checked_at=0,
            key_valid=0,
            key_last_error=None,
        )

    def set_image_key(
        self,
        user_id: int,
        cipher: str,
        suffix: str,
        api_base: str,
        model: str = "",
    ) -> None:
        """配生图 key。和聊天 key 独立，互不影响。

        model 为空表示用默认值（gpt-image-2），客户之后可以用
        /imagemodel 改。这里不覆盖已有的 model，
        否则客户换 key 就得重新选一次模型。
        """
        fields: dict[str, Any] = {
            "image_cipher": cipher,
            "image_suffix": suffix,
            "image_api_base": api_base,
        }
        if model:
            fields["image_model"] = model
        self.update_config(user_id, **fields)

    def clear_image_key(self, user_id: int) -> None:
        self.update_config(
            user_id,
            image_cipher=None,
            image_suffix=None,
            image_api_base=None,
            image_model=None,
        )

    def clear_key(self, user_id: int) -> None:
        self.update_config(
            user_id,
            key_cipher=None,
            key_fingerprint=None,
            key_suffix=None,
            model=None,
            key_checked_at=0,
            key_valid=0,
            key_balance=0,
            key_unlimited=0,
            key_last_error=None,
        )

    def update_check(
        self,
        user_id: int,
        valid: bool,
        balance: int = 0,
        unlimited: bool = False,
        error: str | None = None,
        available_models: list[str] | None = None,
    ) -> None:
        fields: dict = {
            "key_checked_at": now(),
            "key_valid": 1 if valid else 0,
            "key_balance": balance,
            "key_unlimited": 1 if unlimited else 0,
            "key_last_error": error,
        }
        if available_models is not None:
            fields["available_models"] = json.dumps(
                available_models, ensure_ascii=False
            )
        self.update_config(user_id, **fields)

    def get_available_models(self, user_id: int) -> list[str]:
        """读缓存的可用模型列表。空列表表示未知。"""
        cfg = self.get_config(user_id)
        raw = cfg.get("available_models")
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return [str(x) for x in data] if isinstance(data, list) else []
        except Exception:
            return []

    # ------------------------------------------------------------------
    # 消息
    # ------------------------------------------------------------------

    def add_message(
        self, user_id: int, persona_id: str, role: str, content: str
    ) -> int:
        """落库一条消息。调用模型之前必须先调这个。"""
        body = T.scrub(content)
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO messages
                   (user_id, persona_id, role, content, bigrams, tokens, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    user_id,
                    persona_id,
                    role,
                    body,
                    T.bigrams_for_index(body),
                    T.estimate_tokens(body),
                    now(),
                ),
            )
        return int(cur.lastrowid)

    def recent_messages(
        self, user_id: int, persona_id: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            """SELECT role, content, created_at FROM messages
               WHERE user_id=? AND persona_id=?
               ORDER BY id DESC LIMIT ?""",
            (user_id, persona_id, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
        rows.reverse()  # 恢复时间正序
        return rows

    def count_messages(self, user_id: int, persona_id: str | None = None) -> int:
        if persona_id is None:
            cur = self.conn.execute(
                "SELECT COUNT(*) c FROM messages WHERE user_id=?", (user_id,)
            )
        else:
            cur = self.conn.execute(
                "SELECT COUNT(*) c FROM messages WHERE user_id=? AND persona_id=?",
                (user_id, persona_id),
            )
        return int(cur.fetchone()["c"])

    def delete_user_messages(self, user_id: int) -> None:
        """`/forget`：物理删除。隐私权优先。

        向量也一起删——客户说"忘掉我"，不能还留着他的语义指纹。
        """
        with self.conn:
            self.conn.execute(
                "DELETE FROM message_vectors WHERE user_id=?", (user_id,)
            )
            self.conn.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
            self.conn.execute("DELETE FROM summaries WHERE user_id=?", (user_id,))
            self.conn.execute("DELETE FROM facts WHERE user_id=?", (user_id,))

    # ------------------------------------------------------------------
    # 摘要
    # ------------------------------------------------------------------

    def latest_summary(self, user_id: int, persona_id: str) -> dict[str, Any] | None:
        cur = self.conn.execute(
            """SELECT * FROM summaries
               WHERE user_id=? AND persona_id=?
               ORDER BY id DESC LIMIT 1""",
            (user_id, persona_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def add_summary(
        self, user_id: int, persona_id: str, content: str, anchor_id: int, covered_until: int
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO summaries
                   (user_id, persona_id, content, anchor_message_id, covered_until, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (user_id, persona_id, T.scrub(content), anchor_id, covered_until, now()),
            )
        return int(cur.lastrowid)

    # ------------------------------------------------------------------
    # 事实卡
    # ------------------------------------------------------------------

    def get_facts(self, user_id: int, persona_id: str) -> list[dict[str, Any]]:
        """取事实卡。

        persona_id='' 的记录跨人格共享，所以 OR 条件是必需的：
        换人格后仍然记得"用户叫什么"。
        """
        cur = self.conn.execute(
            """SELECT key, value, confidence, source FROM facts
               WHERE user_id=? AND (persona_id='' OR persona_id=?)
               ORDER BY source='manual' DESC, confidence DESC, key""",
            (user_id, persona_id),
        )
        return [dict(r) for r in cur.fetchall()]

    def replace_facts(
        self, user_id: int, persona_id: str, facts: dict[str, str]
    ) -> None:
        """自动抽取结果覆盖事实卡。

        只删 auto 的，manual 的一条都不动——客户手写的必须保得住，
        否则客户认真写了一句"我叫林晚"，聊几轮就被模型冲掉了。
        """
        ts = now()
        with self.conn:
            self.conn.execute(
                "DELETE FROM facts WHERE user_id=? AND persona_id=? AND source='auto'",
                (user_id, persona_id),
            )
            for k, v in facts.items():
                if not k or not v:
                    continue
                # 手动已经写过的 key，自动抽取不许覆盖
                exists = self.conn.execute(
                    "SELECT 1 FROM facts WHERE user_id=? AND persona_id=? AND key=? "
                    "AND source='manual'",
                    (user_id, persona_id, str(k)[:40]),
                ).fetchone()
                if exists:
                    continue
                self.conn.execute(
                    """INSERT OR REPLACE INTO facts
                       (user_id, persona_id, key, value, confidence, source, updated_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (user_id, persona_id, str(k)[:40], str(v)[:200], 0.8, "auto", ts),
                )

    def set_fact(self, user_id: int, key: str, value: str) -> None:
        """客户手动写/改一条事实。永远是 manual，不会被自动抽取覆盖。"""
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO facts
                   (user_id, persona_id, key, value, confidence, source, updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (user_id, "", str(key)[:40], str(value)[:200], 1.0, "manual", now()),
            )

    def delete_fact(self, user_id: int, key: str) -> bool:
        with self.conn:
            n = self.conn.execute(
                "DELETE FROM facts WHERE user_id=? AND persona_id='' AND key=?",
                (user_id, str(key)[:40]),
            ).rowcount
        return n > 0

    def clear_facts(self, user_id: int, keep_manual: bool = False) -> int:
        """清空事实卡。keep_manual=True 只清自动抽取的。"""
        sql = "DELETE FROM facts WHERE user_id=? AND persona_id=''"
        args: tuple = (user_id,)
        if keep_manual:
            sql += " AND source='auto'"
        with self.conn:
            return self.conn.execute(sql, args).rowcount

    # ------------------------------------------------------------------
    # 绑定码
    # ------------------------------------------------------------------

    def create_export_code(self, user_id: int, ttl: int = 1800) -> str:
        """生成数据导出码。独立于绑定码，避免混用。"""
        # 16 位十六进制 = 64 bit。导出的是全部聊天记录，比绑定码敏感，
        # 加长防止被枚举。绑定码只用于绑定，8 位够了。
        code = os.urandom(8).hex().upper()
        ts = now()
        with self.conn:
            self.conn.execute(
                """INSERT INTO export_codes (code, user_id, created_at, expires_at)
                   VALUES (?,?,?,?)""",
                (code, user_id, ts, ts + ttl),
            )
        return code

    def consume_export_code(self, code: str) -> int | None:
        cur = self.conn.execute(
            "SELECT user_id, expires_at, used_at FROM export_codes WHERE code=?",
            (code.upper(),),
        )
        row = cur.fetchone()
        if not row or row["used_at"] is not None:
            return None
        if int(row["expires_at"]) < now():
            return None
        with self.conn:
            upd = self.conn.execute(
                "UPDATE export_codes SET used_at=? WHERE code=? AND used_at IS NULL",
                (now(), code.upper()),
            )
        if upd.rowcount != 1:
            return None
        return int(row["user_id"])

    def create_bind_code(self, user_id: int, ttl: int = 600, kind: str = "chat") -> str:
        code = os.urandom(4).hex().upper()[:6]
        ts = now()
        with self.conn:
            self.conn.execute(
                """INSERT INTO bind_codes
                   (code, user_id, created_at, expires_at, kind)
                   VALUES (?,?,?,?,?)""",
                (code, user_id, ts, ts + ttl, kind),
            )
        return code

    def peek_bind_code(self, code: str) -> int | None:
        """只校验绑定码是否可用，不作废。

        为什么需要"只查看"：key 输错时不应该把码烧掉，
        否则用户要回 QQ 重新要码，体验很差。
        正确顺序是：查看码 → 校验 key → 校验通过才作废。
        """
        cur = self.conn.execute(
            "SELECT user_id, expires_at, used_at FROM bind_codes WHERE code=?",
            (code.upper(),),
        )
        row = cur.fetchone()
        if not row:
            return None
        if row["used_at"] is not None:
            return None
        if int(row["expires_at"]) < now():
            return None
        return int(row["user_id"])

    def peek_bind_info(self, code: str) -> tuple[int, str] | None:
        """返回 (user_id, kind)，码无效时 None。

        和 peek_bind_code 的区别：多带一个 kind，
        绑定网页要据此决定是保存成聊天 key 还是生图 key。
        """
        cur = self.conn.execute(
            "SELECT user_id, expires_at, used_at, kind FROM bind_codes WHERE code=?",
            (code.upper(),),
        )
        row = cur.fetchone()
        if not row:
            return None
        if row["used_at"] is not None:
            return None
        if int(row["expires_at"]) < now():
            return None
        return int(row["user_id"]), (row["kind"] or "chat")

    def consume_bind_code(self, code: str) -> int | None:
        """校验并作废绑定码。返回 user_id，失败返回 None。

        UPDATE 带 used_at IS NULL 条件，保证并发下只有一个请求能成功。
        """
        cur = self.conn.execute(
            "SELECT user_id, expires_at, used_at FROM bind_codes WHERE code=?",
            (code.upper(),),
        )
        row = cur.fetchone()
        if not row:
            return None
        if row["used_at"] is not None:
            return None
        if int(row["expires_at"]) < now():
            return None

        with self.conn:
            upd = self.conn.execute(
                "UPDATE bind_codes SET used_at=? WHERE code=? AND used_at IS NULL",
                (now(), code.upper()),
            )
        if upd.rowcount != 1:
            return None   # 被其他请求抢先用了
        return int(row["user_id"])

    # ------------------------------------------------------------------
    # 用量
    # ------------------------------------------------------------------

    def record_usage(self, user_id: int, tokens_in: int, tokens_out: int) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime())
        with self.conn:
            self.conn.execute(
                """INSERT INTO usage_daily (user_id, day, turns, tokens_in, tokens_out)
                   VALUES (?,?,1,?,?)
                   ON CONFLICT(user_id, day) DO UPDATE SET
                       turns = turns + 1,
                       tokens_in = tokens_in + excluded.tokens_in,
                       tokens_out = tokens_out + excluded.tokens_out""",
                (user_id, day, tokens_in, tokens_out),
            )

    def get_usage(self, user_id: int, days: int = 30) -> dict[str, Any]:
        cur = self.conn.execute(
            """SELECT COALESCE(SUM(turns),0) t, COALESCE(SUM(tokens_in),0) ti,
                      COALESCE(SUM(tokens_out),0) tob
               FROM usage_daily WHERE user_id=?""",
            (user_id,),
        )
        row = cur.fetchone()
        return {
            "turns": int(row["t"]),
            "tokens_in": int(row["ti"]),
            "tokens_out": int(row["tob"]),
        }

    # ------------------------------------------------------------------
    # 任务队列
    # ------------------------------------------------------------------

    def enqueue(self, kind: str, user_id: int, payload: dict[str, Any]) -> int:
        ts = now()
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO jobs (kind, user_id, payload, created_at, updated_at)
                   VALUES (?,?,?,?,?)""",
                (kind, user_id, json.dumps(payload, ensure_ascii=False), ts, ts),
            )
        return int(cur.lastrowid)

    def requeue_stuck_jobs(self, timeout: int = 300) -> int:
        """把卡住的 running 任务放回队列。

        为什么必须有：进程被 kill、机器重启、或任务里死循环时，
        job 会永远停在 running。没有回收，这些任务就永久消失了——
        客户聊了 20 轮却始终没有摘要，而且完全不知道为什么。
        """
        with self.conn:
            cur = self.conn.execute(
                """UPDATE jobs SET status='pending', updated_at=?
                   WHERE status='running' AND updated_at < ?""",
                (now(), now() - timeout),
            )
            n = cur.rowcount
            if n:
                print(f"[jobs] 回收 {n} 个卡住的任务")
            return n

    def claim_job(self) -> dict[str, Any] | None:
        """取一个待处理任务并标记 running。"""
        cur = self.conn.execute(
            "SELECT * FROM jobs WHERE status='pending' ORDER BY id LIMIT 1"
        )
        row = cur.fetchone()
        if not row:
            return None
        job = dict(row)
        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET status='running', attempts=attempts+1, updated_at=? WHERE id=?",
                (now(), job["id"]),
            )
        return job

    def finish_job(self, job_id: int, ok: bool, error: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET status=?, last_error=?, updated_at=? WHERE id=?",
                ("done" if ok else "failed", error, now(), job_id),
            )

    # ------------------------------------------------------------------
    # 人设（多套，可切换）
    # ------------------------------------------------------------------

    def list_personas(self, user_id: int) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT id, name, content, updated_at FROM personas "
            "WHERE user_id=? ORDER BY id",
            (user_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_persona(self, user_id: int, pid: int) -> dict[str, Any] | None:
        cur = self.conn.execute(
            "SELECT id, name, content FROM personas WHERE id=? AND user_id=?",
            (pid, user_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def find_persona_by_name(self, user_id: int, name: str) -> dict[str, Any] | None:
        cur = self.conn.execute(
            "SELECT id, name, content FROM personas WHERE user_id=? AND name=?",
            (user_id, name),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def add_persona(self, user_id: int, name: str, content: str) -> int:
        ts = now()
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO personas (user_id, name, content, created_at, updated_at)
                   VALUES (?,?,?,?,?)""",
                (user_id, name, content, ts, ts),
            )
        return int(cur.lastrowid)

    def update_persona(self, user_id: int, pid: int, name: str, content: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE personas SET name=?, content=?, updated_at=? "
                "WHERE id=? AND user_id=?",
                (name, content, now(), pid, user_id),
            )

    def delete_persona(self, user_id: int, pid: int) -> None:
        """删除人设，并清空它那条对话线。

        必须一起清：messages / summaries / facts。
        只删 messages 会留下孤儿摘要和事实，下次新建同名的人设
        可能撞上旧数据（虽然 id 不同，但排查时非常困惑）。
        """
        line = f"p{pid}"
        with self.conn:
            self.conn.execute(
                "DELETE FROM personas WHERE id=? AND user_id=?", (pid, user_id)
            )
            # 先删向量：message_vectors 有外键指向 messages，
            # 但显式删更保险（外键可能被某些路径关掉）。
            self.conn.execute(
                """DELETE FROM message_vectors
                   WHERE message_id IN (SELECT id FROM messages
                                        WHERE user_id=? AND persona_id=?)""",
                (user_id, line),
            )
            self.conn.execute(
                "DELETE FROM messages WHERE user_id=? AND persona_id=?",
                (user_id, line),
            )
            self.conn.execute(
                "DELETE FROM summaries WHERE user_id=? AND persona_id=?",
                (user_id, line),
            )
            # 这条线专属的事实卡（共享的 persona_id='' 不动）
            self.conn.execute(
                "DELETE FROM facts WHERE user_id=? AND persona_id=?",
                (user_id, line),
            )
            # 清掉这条线上排队的任务，否则任务跑起来找不到人设
            self.conn.execute(
                "DELETE FROM jobs WHERE user_id=? AND payload LIKE ?",
                (user_id, f'%"persona_id": "{line}"%'),
            )

    def rename_persona(self, user_id: int, pid: int, new_name: str) -> bool:
        """改名。名字冲突返回 False。"""
        clash = self.conn.execute(
            "SELECT id FROM personas WHERE user_id=? AND name=? AND id!=?",
            (user_id, new_name, pid),
        ).fetchone()
        if clash:
            return False
        with self.conn:
            self.conn.execute(
                "UPDATE personas SET name=?, updated_at=? WHERE id=? AND user_id=?",
                (new_name, now(), pid, user_id),
            )
        return True

    def active_persona(self, user_id: int) -> dict[str, Any] | None:
        """当前激活的人设。None 表示用默认陪聊风格。"""
        cfg = self.get_config(user_id)
        pid = cfg.get("active_persona_id")
        if not pid:
            return None
        return self.get_persona(user_id, int(pid))

    def set_active_persona(self, user_id: int, pid: int | None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE configs SET active_persona_id=? WHERE user_id=?",
                (pid, user_id),
            )

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------

    def cleanup(self, verbose: bool = True) -> dict[str, int]:
        """清掉过期的一次性数据。

        为什么必须有：这三张表只增不减——
            ① jobs：每条摘要/事实任务一行，20 客户每天 200 行，一年 7 万行
            ② bind_codes：每次 /setkey 一行
            ③ export_codes：每次 /export 一行
        不清的话数据库会一直胖下去，查询也变慢。
        """
        ts = now()
        out: dict[str, int] = {}
        with self.conn:
            # 完成任务保留 7 天（够排查用），失败的多留 30 天
            out["jobs_done"] = self.conn.execute(
                "DELETE FROM jobs WHERE status='done' AND updated_at < ?",
                (ts - 7 * 86400,),
            ).rowcount
            out["jobs_failed"] = self.conn.execute(
                "DELETE FROM jobs WHERE status='failed' AND updated_at < ?",
                (ts - 30 * 86400,),
            ).rowcount
            # 绑定码/导出码过期就没用了，多留 1 天便于排查
            out["bind_codes"] = self.conn.execute(
                "DELETE FROM bind_codes WHERE expires_at < ?", (ts - 86400,)
            ).rowcount
            out["export_codes"] = self.conn.execute(
                "DELETE FROM export_codes WHERE expires_at < ?", (ts - 86400,)
            ).rowcount
        if verbose and any(out.values()):
            print(f"[cleanup] {out}")
        return out

    # ------------------------------------------------------------------
    # 向量
    # ------------------------------------------------------------------

    def save_vector(
        self, message_id: int, user_id: int, vec: bytes, dim: int, model: str
    ) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO message_vectors
                   (message_id, user_id, dim, vec, model, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (message_id, user_id, dim, vec, model, now()),
            )

    def get_vectors(self, user_id: int, limit: int = 2500) -> list[dict[str, Any]]:
        """取某个用户的全部向量。

        只取当前用户：20 个客户每人 5000 条，
        全加载要 100MB，只加载一个 5MB。
        """
        cur = self.conn.execute(
            """SELECT v.message_id, v.dim, v.vec, m.content, m.role, m.created_at
               FROM message_vectors v
               JOIN messages m ON m.id = v.message_id
               WHERE v.user_id = ?
               ORDER BY v.message_id DESC
               LIMIT ?""",
            (user_id, limit),
        )
        return [dict(r) for r in cur.fetchall()]

    def count_vectors(self, user_id: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM message_vectors WHERE user_id=?", (user_id,)
        ).fetchone()[0]

    def messages_without_vector(
        self, user_id: int, limit: int = 200
    ) -> list[dict[str, Any]]:
        """找出还没生成向量的消息（补历史用）。"""
        cur = self.conn.execute(
            """SELECT m.id, m.role, m.content FROM messages m
               WHERE m.user_id = ?
                 AND NOT EXISTS (SELECT 1 FROM message_vectors v
                                 WHERE v.message_id = m.id)
               ORDER BY m.id DESC LIMIT ?""",
            (user_id, limit),
        )
        return [dict(r) for r in cur.fetchall()]

    def backup(self, dest: str) -> None:
        """VACUUM INTO：热备份，不需要停机。

        VACUUM INTO 会包含 WAL 里尚未 checkpoint 的数据，
        所以不会备份出一个"少几条"的文件——这点比自己 cp 强。
        但目标已存在时会直接报错，这里先删掉。
        """
        if os.path.exists(dest):
            os.remove(dest)
        self.conn.execute("VACUUM INTO ?", (dest,))
