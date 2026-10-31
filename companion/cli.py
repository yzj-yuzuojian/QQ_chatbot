"""运维命令。

用法：
    python -m companion.cli gen-secret      # 生成加密密钥
    python -m companion.cli init-db         # 初始化数据库
    python -m companion.cli stats           # 看整体统计
    python -m companion.cli users           # 列出所有用户
    python -m companion.cli backup <dir>    # 热备份
    python -m companion.cli check-keys      # 巡检所有客户 key
    python -m companion.cli assert          # 验证无平台兜底 key
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

from .config import Config
from .db.store import Store
from .llm.provider import Crypto


def cmd_gen_secret() -> None:
    print(Crypto.generate_secret())


def cmd_init_db(cfg: Config) -> None:
    store = Store(cfg.db_path)
    n = store.conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    print(f"数据库已就绪：{cfg.db_path}")
    print(f"现有用户：{n}")
    store.close()


def cmd_stats(cfg: Config) -> None:
    store = Store(cfg.db_path)
    c = store.conn

    total_users = c.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    bound = c.execute(
        "SELECT COUNT(*) c FROM configs WHERE key_cipher IS NOT NULL"
    ).fetchone()["c"]
    with_model = c.execute(
        "SELECT COUNT(*) c FROM configs WHERE model IS NOT NULL AND model != ''"
    ).fetchone()["c"]
    msgs = c.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
    facts = c.execute("SELECT COUNT(*) c FROM facts").fetchone()["c"]
    sums = c.execute("SELECT COUNT(*) c FROM summaries").fetchone()["c"]
    jobs_p = c.execute("SELECT COUNT(*) c FROM jobs WHERE status='pending'").fetchone()["c"]
    jobs_f = c.execute("SELECT COUNT(*) c FROM jobs WHERE status='failed'").fetchone()["c"]

    print("=" * 40)
    print(f"用户总数      {total_users}")
    print(f"已绑定 key    {bound}  ({bound/max(total_users,1)*100:.0f}%)")
    print(f"已选模型      {with_model}")
    print("-" * 40)
    print(f"消息总数      {msgs}")
    print(f"事实卡条目    {facts}")
    print(f"摘要条数      {sums}")
    print("-" * 40)
    print(f"待处理任务    {jobs_p}")
    print(f"失败任务      {jobs_f}")

    size = os.path.getsize(cfg.db_path) / 1024 / 1024 if os.path.exists(cfg.db_path) else 0
    print(f"数据库大小    {size:.1f} MB")
    print("=" * 40)
    store.close()


def cmd_users(cfg: Config) -> None:
    store = Store(cfg.db_path)
    rows = store.conn.execute(
        """SELECT u.id, u.created_at, u.status,
                  c.key_suffix, c.model, c.persona_id, c.key_valid,
                  (SELECT COUNT(*) FROM messages m WHERE m.user_id=u.id) msgs
           FROM users u LEFT JOIN configs c ON c.user_id=u.id
           ORDER BY u.id"""
    ).fetchall()

    print(f"{'ID':<4} {'状态':<8} {'消息':<6} {'key':<12} {'模型':<28} {'人格'}")
    print("-" * 80)
    for r in rows:
        key = f"****{r['key_suffix']}" if r["key_suffix"] else "(未绑定)"
        model = (r["model"] or "(未选)")[:26]
        print(
            f"{r['id']:<4} {r['status']:<8} {r['msgs']:<6} {key:<12} {model:<28} {r['persona_id']}"
        )
    store.close()


def cmd_backup(cfg: Config, dest_dir: str) -> None:
    os.makedirs(dest_dir, exist_ok=True)
    name = time.strftime("companion-%Y%m%d-%H%M%S.db")
    dest = os.path.join(dest_dir, name)
    store = Store(cfg.db_path)
    store.backup(dest)
    store.close()
    size = os.path.getsize(dest) / 1024 / 1024
    print(f"已备份到 {dest} ({size:.1f} MB)")


async def cmd_check_keys(cfg: Config) -> None:
    """巡检所有客户 key。失效的列出来，供人工通知。"""
    import aiohttp

    store = Store(cfg.db_path)
    crypto = Crypto(cfg.secret)

    rows = store.conn.execute(
        "SELECT user_id, key_cipher, api_base FROM configs WHERE key_cipher IS NOT NULL"
    ).fetchall()

    ok = bad = 0
    bad_list: list[tuple[int, str]] = []

    async with aiohttp.ClientSession() as sess:
        for r in rows:
            uid = int(r["user_id"])
            try:
                key = crypto.decrypt(r["key_cipher"])
            except Exception:
                bad += 1
                bad_list.append((uid, "解密失败"))
                continue

            from .llm.provider import check_key

            info = await check_key(sess, r["api_base"] or cfg.api_base, key)
            if info.valid:
                ok += 1
                store.update_check(
                    uid,
                    valid=True,
                    balance=info.balance,
                    unlimited=info.unlimited,
                )
            else:
                bad += 1
                bad_list.append((uid, info.error or "未知原因"))
                store.update_check(uid, valid=False, error=info.error)

    print(f"有效 {ok}，失效 {bad}")
    for uid, reason in bad_list:
        print(f"  用户 {uid}: {reason}")

    store.close()


def cmd_assert() -> None:
    """验证没有任何平台级兜底 key。"""
    from .llm.provider import ProviderGate

    try:
        ProviderGate.assert_no_default_provider()
        print("✅ 通过：没有检测到平台级 LLM key")
    except RuntimeError as e:
        print(f"❌ 失败：{e}")
        sys.exit(1)


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]
    cfg = Config.load()

    if cmd == "gen-secret":
        cmd_gen_secret()
    elif cmd == "init-db":
        cmd_init_db(cfg)
    elif cmd == "stats":
        cmd_stats(cfg)
    elif cmd == "users":
        cmd_users(cfg)
    elif cmd == "backup":
        dest = sys.argv[2] if len(sys.argv) > 2 else "/var/backups/qq-companion"
        cmd_backup(cfg, dest)
    elif cmd == "check-keys":
        asyncio.run(cmd_check_keys(cfg))
    elif cmd == "assert":
        cmd_assert()
    else:
        print(f"未知命令：{cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
