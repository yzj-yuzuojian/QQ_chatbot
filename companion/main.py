"""入口：启动网关 + 绑定页 + 异步任务循环。

启动顺序很重要：
    1. 断言没有平台级 key（第 ③ 层拦截）
    2. 打开数据库
    3. 启动后台任务循环
    4. 启动绑定页
    5. 启动 QQ 网关
"""
from __future__ import annotations

import asyncio
import signal
import sys
import time
from typing import Any

import aiohttp
from aiohttp import web

from .commands.router import Router
from .config import Config
from .db.store import Store
from .engine import Engine
from .llm.provider import Crypto, ProviderGate
from .memory import vector as V
from .qq.gateway import Gateway, Sender
from .web.bind import make_app


def log(msg: str) -> None:
    sys.stderr.write(f"{time.strftime('%m-%d %H:%M:%S')} {msg}\n")
    sys.stderr.flush()


class App:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.store = Store(cfg.db_path)
        self.crypto = Crypto(cfg.secret)
        self.session: aiohttp.ClientSession | None = None
        self.gate: ProviderGate | None = None
        self.engine: Engine | None = None
        self.router: Router | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        # ---- 第 ③ 层拦截：启动断言 ----
        ProviderGate.assert_no_default_provider()
        log("[guard] 已确认无平台级兜底 key")

        if not self.crypto.enabled:
            log("⚠️  未配置 COMPANION_SECRET，客户 key 将以明文存储")

        self.session = aiohttp.ClientSession()
        self.gate = ProviderGate(self.store, self.crypto, self.session, log)
        self._current_sender: Sender | None = None
        self.engine = Engine(
            self.store,
            self.gate,
            self.session,
            log,
            embed_cfg={
                "enabled": self.cfg.embed_enabled,
                "api_base": self.cfg.embed_api_base,
                "api_key": self.cfg.embed_api_key,
                "model": self.cfg.embed_model,
            },
            crypto=self.crypto,
            sender_provider=lambda: self._current_sender,
            default_api_base=self.cfg.api_base,
        )
        self.router = Router(
            self.store,
            self.gate,
            self.engine,
            self.session,
            self.crypto,
            self.cfg.api_base,
            self.cfg.bind_url,
            log,
            site_url=self.cfg.resolved_site_url(),
        )

        # 绑定页
        app = make_app(
            self.store, self.session, self.crypto, self.cfg.api_base, log
        )
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.cfg.bind_host, self.cfg.bind_port)
        await site.start()
        log(f"[web] 绑定页 http://{self.cfg.bind_host}:{self.cfg.bind_port}/")

        # 后台任务
        for i in range(self.cfg.workers):
            asyncio.create_task(self._job_loop(i))

        # QQ 网关
        gw = Gateway(self.cfg.app_id, self.cfg.app_secret, self._on_message, log)
        log("[boot] 启动 QQ 网关")
        await gw.run()

    async def _on_message(self, data: dict[str, Any], sender: Sender) -> None:
        """处理一条 QQ 消息。"""
        event_type = (
            "group"
            if "group_openid" in data
            else "c2c"
        )

        if event_type == "group":
            target = {"type": "group", "id": data.get("group_openid", "")}
            openid = (data.get("member") or {}).get("member_openid", "")
            chat_type = "group"
        else:
            target = {"type": "c2c", "id": (data.get("author") or {}).get("user_openid", "")}
            openid = target["id"]
            chat_type = "c2c"

        text = (data.get("content") or "").strip()
        msg_id = data.get("id", "")

        if not text or not target["id"] or not openid:
            return

        assert self.router

        # 建用户也要包在 try 里：数据库繁忙（锁等待超时）时
        # 这里抛异常会让客户完全收不到回复，连"稍后再试"都没有。
        try:
            user_id = self.store.get_or_create_user(self.cfg.app_id, openid)
        except Exception as e:
            log(f"[user] {type(e).__name__}: {e!r}")
            try:
                await sender.reply(target, msg_id, "我这边有点卡，稍后再发一次？")
            except Exception:
                pass
            return

        log(f"[in] {chat_type} uid={user_id} {text[:30]!r}")

        try:
            answer, _ = await self.router.handle(
                user_id, text, chat_type, self.cfg.app_id, target, openid, msg_id
            )
        except Exception as e:
            log(f"[router] {type(e).__name__}: {e!r}")
            answer = "出了点问题，稍后再试。"

        # 切分上限必须和网关的一致。原来网关 1800、这里 1500，
        # 两边不一致时超出部分会被截掉且不报错。
        from companion.qq.gateway import MAX_CHARS as QQ_MAX

        for chunk in _split(answer, QQ_MAX - 100):
            try:
                await sender.reply(target, msg_id, chunk)
            except Exception as e:
                log(f"[send] {type(e).__name__}: {e!r}")
                break

    async def _job_loop(self, idx: int) -> None:
        """异步任务循环：抽事实 + 更新摘要。

        必须走 gate.resolve，客户 key 失效时任务直接失败，
        绝不能回落到任何平台 key。
        """
        assert self.engine
        # 启动时先把上次崩溃留下的 running 任务放回队列
        try:
            self.store.requeue_stuck_jobs()
        except Exception as e:
            log(f"[job{idx}] 回收任务失败: {e!r}")

        # 启动时给历史消息补向量（老客户升级后才有的能力）。
        # 只补最近 200 条——再早的消息已经进了摘要，检索优先级低。
        try:
            await self._backfill_vectors()
        except Exception as e:
            log(f"[vector] 补历史失败: {e!r}")

        tick = 0
        last_cleanup = 0.0
        last_vecfix = 0.0
        while not self._stop.is_set():
            try:
                # 每 20 轮（约 1 分钟）检查一次有没有卡死的任务
                tick += 1
                if tick % 20 == 0:
                    self.store.requeue_stuck_jobs()

                # 每天清理一次过期数据（jobs / 各种码）
                import time as _t
                if _t.time() - last_cleanup > 86400:
                    last_cleanup = _t.time()
                    self.store.cleanup()

                # 每 10 分钟补一次漏掉的向量。
                #
                # 为什么需要：聊天时向量是后台任务算的，
                # embedding 服务抖一下、超时一次，那条消息就永久缺向量了，
                # 检索时永远找不到它。只在启动补一次不够——
                # 服务可能连跑很多天不重启。
                if _t.time() - last_vecfix > 600:
                    last_vecfix = _t.time()
                    try:
                        await self._backfill_vectors(use_lock=False)
                    except Exception as e:
                        log(f"[vector] 周期补漏失败: {e!r}")

                job = self.store.claim_job()
                if not job:
                    await asyncio.sleep(3)
                    continue
                log(f"[job{idx}] {job['kind']} user={job['user_id']}")
                await self.engine.run_job(job)
            except Exception as e:
                log(f"[job{idx}] {type(e).__name__}: {e!r}")
                await asyncio.sleep(5)

    async def _backfill_vectors(self, use_lock: bool = True) -> None:
        """给还没有向量的历史消息补上。逐用户处理，避免一次加载太多。

        多个 worker 会同时跑这里，所以先抢一个文件锁——
        否则 4 个 worker 会把同一批消息各算一遍，
        白花 4 倍 embedding 调用（虽然免费，但没必要）。
        """
        assert self.engine
        if not self.engine._vector_ready:
            return
        # 启动时的补历史要抢锁（4 个 worker 别重复干同一批活）。
        # 周期补漏不抢锁——它补的是"最近漏掉的那几条"，
        # 各 worker 抢到的是不同用户的活，重复的概率极低，
        # 而抢锁失败会让漏掉的消息一直补不上。
        if use_lock:
            import fcntl

            lock_path = (self.cfg.db_path or "/tmp/x.db") + ".vecbackfill.lock"
            try:
                self._vec_lock = open(lock_path, "w")
                fcntl.flock(self._vec_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except Exception:
                log("[vector] 另一个 worker 正在补，跳过")
                return
        base, key, model = self.engine._embed_args()
        users = self.store.conn.execute(
            """SELECT DISTINCT m.user_id FROM messages m
               WHERE NOT EXISTS (SELECT 1 FROM message_vectors v
                                 WHERE v.message_id = m.id)"""
        ).fetchall()
        if not users:
            return
        total = 0
        for row in users:
            uid = int(row["user_id"])
            n = await V.index_messages(
                self.store, self.session, base, key, uid, limit=200, model=model
            )
            total += n
            if n:
                log(f"[vector] user={uid} 补了 {n} 条")
        if total:
            log(f"[vector] 历史补齐完成，共 {total} 条")

    def stop(self) -> None:
        self._stop.set()


def _split(text: str, limit: int) -> list[str]:
    """按长度切分，尽量在换行/句号处断开。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break

        window = rest[:limit]
        cut = max(
            window.rfind("\n"),
            window.rfind("。"),
            window.rfind("！"),
            window.rfind("？"),
        )
        if cut < limit // 2:
            cut = limit
        else:
            cut += 1

        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip()

    return chunks


async def amain() -> None:
    cfg = Config.load()

    errs = cfg.validate()
    for e in errs:
        log(f"⚠️  {e}")
    if not cfg.app_id or not cfg.app_secret:
        sys.exit("缺少 QQ 凭据，无法启动")

    app = App(cfg)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, app.stop)
        except NotImplementedError:
            pass

    try:
        await app.start()
    finally:
        if app.session:
            await app.session.close()
        app.store.close()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
