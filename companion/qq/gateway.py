"""QQ 官方 Bot WebSocket 网关。

复用 `qq-cs` 的接入逻辑，但做了两处调整：
  1. 新增 openid → 内部 user_id 的映射（不直接用 openid 当主键）
  2. 命令优先于对话，且拦截逻辑前置
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
import time
from typing import Any, Awaitable, Callable

import aiohttp
import websockets

API = "https://api.bot.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"

# 1<<25 群@消息 / 1<<30 单聊消息 / 1<<0 频道基础
INTENTS = (1 << 25) | (1 << 30) | (1 << 0)

MAX_CHARS = 1800


class Gateway:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        on_message: Callable[[dict[str, Any], "Sender"], Awaitable[None]],
        log: Callable[[str], None] = print,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.on_message = on_message
        self.log = log
        self._token = ""
        self._expire_at = 0.0
        self.session: aiohttp.ClientSession | None = None
        # 事件去重（断线重连后 QQ 会重投）
        self._seen_ids: set[str] = set()
        self._seen_order: deque[str] = deque()
        # 并发上限：20 个客户同时说话不该开 20 个无上限任务，
        # 否则一次消息洪水能把内存和上游接口打满。
        self._sem = asyncio.Semaphore(20)

    async def token(self) -> str:
        now = time.time()
        if self._token and now < self._expire_at - 60:
            return self._token

        assert self.session
        async with self.session.post(
            TOKEN_URL,
            json={"appId": self.app_id, "clientSecret": self.app_secret},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            data = await r.json()

        tok = data.get("access_token")
        if not tok:
            raise RuntimeError(f"获取 access_token 失败：{data}")

        self._token = tok
        self._expire_at = now + int(data.get("expires_in", 7200))
        return tok

    async def run(self) -> None:
        async with aiohttp.ClientSession() as sess:
            self.session = sess
            sender = Sender(self, sess)
            await self._gateway_loop(sender)

    async def _gateway_loop(self, sender: "Sender") -> None:
        while True:
            try:
                tok = await self.token()
                async with self.session.get(
                    f"{API}/gateway", headers={"Authorization": f"QQBot {tok}"}
                ) as r:
                    gw = await r.json()

                url = gw.get("url")
                if not url:
                    raise RuntimeError(f"获取 gateway 失败：{gw}")

                self.log(f"[gw] 连接 {url[:40]}…")
                async with websockets.connect(
                    url, max_size=2**22, ping_interval=20
                ) as ws:
                    seq: int | None = None
                    while True:
                        ev = json.loads(await ws.recv())
                        op = ev.get("op")

                        if op == 10:
                            interval = (
                                ev.get("d", {}).get("heartbeat_interval", 30000)
                                / 1000
                            )
                            await ws.send(
                                json.dumps(
                                    {
                                        "op": 2,
                                        "d": {
                                            "token": f"QQBot {tok}",
                                            "intents": INTENTS,
                                            "shard": [0, 1],
                                            "properties": {},
                                        },
                                    }
                                )
                            )
                            asyncio.create_task(
                                self._heartbeat(ws, interval, lambda: seq)
                            )

                        elif op == 0:
                            seq = ev.get("s", seq)
                            et = ev.get("t", "")
                            if et == "READY":
                                self.log("[gw] READY 登录成功")
                            elif et in (
                                "GROUP_AT_MESSAGE_CREATE",
                                "C2C_MESSAGE_CREATE",
                            ):
                                # 去重：QQ 在断线重连后会重投事件，
                                # 不去重就会重复回复、重复扣客户的钱。
                                did = str((ev.get("d") or {}).get("id") or "")
                                if did and self._seen(did):
                                    self.log(f"[gw] 重复事件已跳过 {did[:12]}")
                                    continue
                                asyncio.create_task(
                                    self._dispatch(ev, et, sender)
                                )

                        elif op in (7, 9):
                            self.log(f"[gw] op={op} 重连")
                            break

            except Exception as e:
                self.log(f"[gw] 异常 {type(e).__name__}: {e!r}，5 秒后重试")

            await asyncio.sleep(5)

    def _seen(self, event_id: str) -> bool:
        """事件 id 去重。True 表示见过（应跳过）。

        用一个固定容量的 deque + set：只记最近 2000 个，
        超出就淘汰最老的。永久保存会无限占内存。
        """
        if event_id in self._seen_ids:
            return True
        self._seen_ids.add(event_id)
        self._seen_order.append(event_id)
        while len(self._seen_order) > 2000:
            old = self._seen_order.popleft()
            self._seen_ids.discard(old)
        return False

    async def _dispatch(
        self, ev: dict[str, Any], event_type: str, sender: "Sender"
    ) -> None:
        data = ev.get("d") or {}
        async with self._sem:
            try:
                await self.on_message(data, sender)
            except Exception as e:
                self.log(f"[handler] {type(e).__name__}: {e!r}")

    async def _heartbeat(
        self, ws: Any, interval: float, get_seq: Callable[[], int | None]
    ) -> None:
        while True:
            try:
                await asyncio.sleep(interval)
                await ws.send(json.dumps({"op": 1, "d": get_seq()}))
            except Exception:
                return


class Sender:
    """发消息。默认走被动回复。

    【硬规则】所有发送都必须尽量带 msg_id，走被动回复。

    为什么这条规则重要：
        QQ 两种发消息的配额完全不同——
            被动回复：客户发 1 条，你能回 4 条。用不完。
            主动消息：每月总量很少（几十条量级），用一条少一条。
        陪伴机器人每条回复都发消息，如果走主动消息，
        几天就能把整月配额吃光，而且吃光后**客户会直接收不到回复**。

    唯一允许不带 msg_id 的情况：
        ① 客户消息超过 60 分钟（被动窗口过期），且降级重试仍失败
        ② 将来做定时提醒（但那应该走另一套专门逻辑，不是聊天）

    代码里有日志兜底：一旦走了主动消息，日志会打
        [send] ⚠️ 无 msg_id，走主动消息（吃配额）
    上线后如果看到这一行，说明有调用点漏传 msg_id，要查。

    单聊被动回复有效期 60 分钟。
    """

    def __init__(self, gw: Gateway, session: aiohttp.ClientSession) -> None:
        self.gw = gw
        self.session = session
        # msg_id → 已用过的最大 seq。
        # 必须设上限：每条消息都会留一个 key，跑一个月就是几万个，
        # 而且永远不会被用到第二次（回复窗口只有 60 分钟）。
        self._seq: dict[str, int] = {}
        self._seq_order: deque[str] = deque()

    def next_seq(self, msg_id: str) -> int:
        """同一条消息的多次回复要递增 msg_seq，否则会去重。"""
        n = self._seq.get(msg_id, 0) + 1
        if msg_id not in self._seq:
            self._seq_order.append(msg_id)
            # 2000 条消息足够覆盖 60 分钟窗口内所有活跃会话
            while len(self._seq_order) > 2000:
                old = self._seq_order.popleft()
                self._seq.pop(old, None)
        self._seq[msg_id] = n
        return n

    async def send_image(
        self,
        target: dict[str, Any],
        openid: str,
        data: bytes,
        msg_id: str = "",
    ) -> bool:
        """发一张图片。返回是否成功。

        优先走被动回复（带 msg_id），失败再退到主动消息。

        为什么要优先被动：
            QQ 的配额规则不一样——
                被动回复：客户每发一条，就能回 4 条（同一 msg_id）
                主动消息：每月总量很少，几条到几十条不等
            生图要 30-60 秒，但被动的回复窗口有 **60 分钟**，
            完全够用。docstring 以前写"窗口可能过了"是想当然，
            实际上 60 分钟的窗口对 60 秒的生图绰绰有余。
            改用被动后，生图不消耗宝贵的主动配额。

        两步：① 上传拿 file_info ② 发 msg_type=7 的富媒体消息。
        """
        from companion.media import image as I

        if not data:
            return False

        tok = await self.gw.token()
        is_group = target.get("type") == "group"
        gid = target.get("id", "")
        chat_id = openid or gid

        file_info = await I.upload_to_qq(
            self.session, tok, chat_id, data, is_group=is_group
        )
        if not file_info or file_info.startswith("__ERR__"):
            self.gw.log(f"[image] 上传失败 {file_info[:120]}")
            return False

        if is_group:
            url = f"{API}/v2/groups/{gid}/messages"
        else:
            url = f"{API}/v2/users/{openid}/messages"

        async def _post(payload: dict[str, Any]) -> tuple[int, str]:
            async with self.session.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"QQBot {tok}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                return r.status, (await r.text() if r.status >= 400 else "")

        # ① 优先被动回复：带 msg_id + 递增 seq，不消耗主动配额
        if msg_id:
            payload = {
                "content": "",
                "msg_type": 7,
                "media": {"file_info": file_info},
                "msg_id": msg_id,
                # 文字已经用过 seq=1，这里必须往后排，
                # 否则 QQ 会当成重复消息丢掉。
                "msg_seq": self.next_seq(msg_id),
            }
            status, body = await _post(payload)
            if status < 400:
                return True
            # 不管是 msg_id 过期还是别的错，都退主动消息——
            # 图片已经生成好了（花了钱和时间），不能因为发送方式失败就丢掉。
            self.gw.log(f"[image] 被动发送失败 {status} {body[:120]}，改试主动")

        # ② 退到主动消息。这才会消耗配额，但没有 msg_id 时是唯一办法。
        if not msg_id:
            self.gw.log("[image] ⚠️ 无 msg_id，图片走主动消息（吃配额）")
        payload = {
            "content": "",
            "msg_type": 7,
            "media": {"file_info": file_info},
            "msg_seq": 1,
        }
        status, body = await _post(payload)
        if status >= 400:
            self.gw.log(f"[image] 发送失败 {status} {body[:150]}")
            return False
        return True

    async def reply(self, target: dict[str, Any], msg_id: str, content: str) -> None:
        if not content.strip():
            return

        tok = await self.gw.token()
        if target["type"] == "group":
            url = f"{API}/v2/groups/{target['id']}/messages"
        else:
            url = f"{API}/v2/users/{target['id']}/messages"

        # 不在这里截断——切分已经在 main._split 里做过了。
        # 这里再截一次会悄悄吞掉内容，客户完全不知道少了什么。
        # 万一真超了，宁可让 QQ 报错（日志能看到），也不要静默丢内容。
        payload: dict[str, Any] = {"content": content, "msg_type": 0}
        if msg_id:
            # 被动回复：不吃主动配额
            payload["msg_id"] = msg_id
            payload["msg_seq"] = self.next_seq(msg_id)
        else:
            # 没有 msg_id 就别塞空字符串——QQ 会报"msg_id无效"。
            # 退化成主动消息，这是唯一能发的办法。
            #
            # 但正常流程不该走到这里：主动消息每月配额很少，
            # 被聊天吃掉就等于没法做提醒。所以留一条日志，
            # 真出现了能立刻发现是哪个调用点漏传了。
            self.gw.log(f"[send] ⚠️ 无 msg_id，走主动消息（吃配额）: {content[:30]!r}")
            payload["msg_seq"] = 1

        async with self.session.post(
            url,
            json=payload,
            headers={
                "Authorization": f"QQBot {tok}",
                "Content-Type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status >= 400:
                body = await r.text()
                # msg_id 过期（被动窗口 60 分钟）时自动改主动消息。
                # 生图要 30-60 秒，正常不会过期；但服务重启、
                # 队列积压的时候可能拖过窗口，那时不能静默失败——
                # 客户在等一条消息，什么都不回比迟到更糟。
                if msg_id and "msg_id" in body and r.status in (400, 403):
                    self.gw.log(
                        f"[send] msg_id 失效，改主动消息: {body[:100]}"
                    )
                    payload.pop("msg_id", None)
                    payload["msg_seq"] = 1
                    async with self.session.post(
                        url,
                        json=payload,
                        headers={
                            "Authorization": f"QQBot {tok}",
                            "Content-Type": "application/json",
                        },
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as r2:
                        if r2.status >= 400:
                            body2 = await r2.text()
                            self.gw.log(f"[send] 重试仍失败 {r2.status} {body2[:150]}")
                    return
                self.gw.log(f"[send] {r.status} {body[:150]}")
