"""对话引擎：把记忆、检索、人格、provider 串起来。

单轮流程（见方案文档 §2、§5）：
    ① 落库（调模型之前）
    ② 判断是否检索
    ③ 组装 prompt（角色卡 → 事实 → 摘要 → 检索 → 最近对话）
    ④ 选 provider（含拦截）
    ⑤ 调模型
    ⑥ 回发 + 触发异步任务
"""
from __future__ import annotations

import asyncio
from typing import Any

from . import text as T
from .db.store import Store
from .llm import client as LLM
from .llm.client import LLMError
from .llm.provider import NoProvider, ProviderGate
from .memory import facts as F
from .memory import retrieve as R
from .memory import rewrite as RW
from .memory import summary as S
from .persona import cards as C

# 最近对话保留多少条
RECENT_LIMIT = 10

# 回复最大长度（QQ 单条 1800，留余量给分段）
MAX_REPLY = 1500


class Engine:
    def __init__(
        self,
        store: Store,
        gate: ProviderGate,
        session: Any,
        log: Any = print,
        embed_cfg: dict[str, Any] | None = None,
        crypto: Any = None,
        sender_provider: Any = None,
        default_api_base: str = "",
    ) -> None:
        self.store = store
        self.gate = gate
        self.session = session
        self.log = log
        # 生图要解密客户 key，还要能发消息回客户。
        # 用 provider 而不是直接持有 Sender：Sender 是每次连接新建的，
        # 持有旧对象会发不出去。
        self.crypto = crypto or getattr(gate, "crypto", None)
        self.sender_provider = sender_provider
        self.default_api_base = default_api_base
        # 向量配置（平台统一 key，客户无感）。
        # 不传 = 关闭向量，退回纯关键词检索。
        self.embed_cfg = embed_cfg or {}
        self._vector_ready = bool(
            self.embed_cfg.get("enabled") and self.embed_cfg.get("api_key")
        )
        self._log_vec_cfg()

    async def _run_draw(self, user_id: int, data: dict[str, Any]) -> None:
        """生成图片并发给客户。

        失败必须让客户知道——不能沉默。
        """
        from companion.media import image as IM

        prompt = (data.get("prompt") or "").strip()
        target = data.get("target") or {}
        openid = data.get("openid") or ""
        # 客户那条消息的 msg_id。拿着它就能走被动回复，不吃主动配额——
        # 图片、报错文字都该用它。之前这里漏了，报错走了主动消息。
        msg_id = data.get("msg_id") or ""
        if not prompt or not target:
            self.log("[draw] 参数不全，跳过")
            return

        cfg = self.store.get_config(user_id)
        cipher = cfg.get("image_cipher")
        if not cipher:
            self.log("[draw] 客户没配生图 key")
            return

        try:
            key = self.crypto.decrypt(cipher)
        except Exception as e:
            self.log(f"[draw] key 解密失败: {e!r}")
            return

        # 客户没单独配生图站时，用默认中转站。
        # 之前这里写死了具体域名，开源后别人的站就调不通了。
        base = cfg.get("image_api_base") or self.default_api_base
        model = cfg.get("image_model") or IM.DEFAULT_IMAGE_MODEL

        self.log(f"[draw] user={user_id} model={model} prompt={prompt[:40]!r}")
        imgs, err = await IM.generate(self.session, base, key, prompt, model=model)

        sender = self.sender_provider() if self.sender_provider else None
        if not imgs:
            self.log(f"[draw] 失败: {err}")
            if sender:
                # 报错也走被动回复，用同一个 msg_id、seq 继续往后排。
                await sender.reply(target, msg_id, f"画不出来：{err}")
            return

        ok = False
        if sender:
            for i, blob in enumerate(imgs[:2]):
                ok = (
                    await sender.send_image(target, openid, blob, msg_id=msg_id)
                    or ok
                )
        if not ok:
            self.log("[draw] 图片发送失败")
        else:
            self.log(f"[draw] 已发送 {len(imgs)} 张")

    def _log_vec_cfg(self) -> None:
        if not self.embed_cfg.get("enabled"):
            self.log("[vector] 已关闭（配置里 embed_enabled=false）")
            return
        if not self.embed_cfg.get("api_key"):
            self.log("[vector] 未配置 key，语义检索不可用，退回关键词")
            return
        self.log(
            f"[vector] 启用 {self.embed_cfg.get('model', 'bge-m3')} "
            f"@ {self.embed_cfg.get('api_base', '')}"
        )

    def _embed_args(self) -> tuple[str, str, str]:
        return (
            self.embed_cfg.get("api_base", ""),
            self.embed_cfg.get("api_key", ""),
            self.embed_cfg.get("model", "BAAI/bge-m3"),
        )

    async def index_vector(self, user_id: int, message_id: int, content: str) -> None:
        """给单条消息生成向量。失败静默——向量是锦上添花，不能影响聊天。"""
        if not self._vector_ready:
            return
        try:
            from companion.memory import vector as V

            base, key, model = self._embed_args()
            await V.index_message_one(
                self.store, self.session, base, key, user_id, message_id, content, model
            )
        except Exception as e:
            self.log(f"[vector] 生成失败 {type(e).__name__}: {e!r}")

    async def reply(
        self,
        user_id: int,
        line_id: str,
        user_text: str,
        modifier_id: str = "normal",
        custom_persona: str = "",
    ) -> str:
        """生成一条回复。异常统一转成用户可读的话术。"""

        # ① 先落库。花钱之前必须有记录，否则钱花了还查不到。
        user_msg_id = self.store.add_message(user_id, line_id, "user", user_text)
        # 向量异步生成：放后台任务，绝不阻塞回复。
        # 生成失败不影响聊天，下次检索退回关键词。
        if self._vector_ready:
            asyncio.create_task(self.index_vector(user_id, user_msg_id, user_text))

        # ② 判断是否检索
        recall_block = ""
        prov: dict | None = None
        if R.should_retrieve(user_text):
            extra: list[str] = []
            try:
                # 先看是否需要改写。必须在 resolve 之前判断，
                # 因为改写要花客户的钱，得先确认 provider 可用。
                need_rw = R.needs_rewrite(self.store, user_id, user_text)
                prov = await self.gate.resolve(user_id)

                if need_rw:
                    extra = await RW.rewrite(
                        self.session,
                        prov["api_base"],
                        prov["api_key"],
                        prov["model"],
                        user_text,
                    )
                    if extra:
                        self.log(f"[rewrite] {user_text!r} -> {extra}")

                # 混合检索：关键词 + 向量。
                # hybrid_search 内部已处理向量失败的情况（返回纯关键词结果），
                # 所以 embedding 服务挂了也不会让客户聊不了天。
                # 向量走**平台 key**（客户 key 里没有 embedding 模型）。
                # 没配就自动只用关键词。
                base, ekey, emodel = self._embed_args()
                hits = await R.hybrid_search(
                    self.store,
                    self.session,
                    base if ekey else "",
                    ekey,
                    user_id,
                    user_text,
                    line_id,
                    extra_terms=extra,
                    embed_model=emodel,
                )
                recall_block = R.format_for_prompt(hits)
            except NoProvider:
                raise
            except Exception as e:
                self.log(f"[retrieve] {type(e).__name__}: {e!r}")

        # ③ 组装 prompt
        facts = self.store.get_facts(user_id, line_id)
        summ = self.store.latest_summary(user_id, line_id)

        from companion.media.image import image_hint

        system_prompt = C.build_system_prompt(
            persona=custom_persona,
            modifier_id=modifier_id,
            facts_block=F.render(facts),
            summary_block=S.render(summ),
            recall_block=recall_block,
            extra_rules=image_hint(),
        )

        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt}
        ]
        for m in self.store.recent_messages(user_id, line_id, RECENT_LIMIT):
            messages.append({"role": m["role"], "content": m["content"]})
        messages.append({"role": "user", "content": user_text})

        # ④ 选 provider —— ② 里已经取过就复用，避免重复校验
        if prov is None:
            prov = await self.gate.resolve(user_id)

        # ⑤ 调模型
        try:
            resp = await LLM.chat(
                self.session,
                prov["api_base"],
                prov["api_key"],
                prov["model"],
                messages,
                timeout=120,
            )
        except LLMError:
            raise

        answer = resp.content.strip()[:MAX_REPLY]
        if not answer:
            answer = "……"

        # 拆出画图指令。模型把"要画什么"写在单独的标记行里，
        # 这里抠出来交给上层去调生图接口。
        from companion.media.image import extract_draw

        answer, draw_prompt = extract_draw(answer)
        if not answer:
            # 模型只写了标记没写人话，补一句，否则会发空消息
            answer = "……"

        # ⑥ 落库回复
        asst_msg_id = self.store.add_message(user_id, line_id, "assistant", answer)
        if self._vector_ready:
            asyncio.create_task(self.index_vector(user_id, asst_msg_id, answer))
        self.store.record_usage(user_id, resp.tokens_in, resp.tokens_out)

        # 异步任务：抽取事实 + 更新摘要。
        # 注意：这些任务也用**客户自己的 key**，不能用平台 key（见 3 层拦截）。
        self.store.enqueue(
            "facts",
            user_id,
            {"persona_id": line_id},
        )
        self.store.enqueue(
            "summary",
            user_id,
            {"persona_id": line_id},
        )

        return answer, draw_prompt

    async def run_job(self, job: dict[str, Any]) -> None:
        """执行异步任务。必须走 gate.resolve，否则会偷用平台 key。"""
        user_id = int(job["user_id"])
        payload = job.get("payload") or "{}"
        try:
            import json

            data = json.loads(payload)
        except Exception:
            data = {}

        line_id = data.get("persona_id") or "main"
        kind = job["kind"]

        # 生图任务走自己的 key，不碰聊天 key。
        # 必须在 resolve 之前拦截——否则客户没配聊天 key 时，
        # 生图任务会因为 resolve 失败被丢掉，而它本来不需要聊天 key。
        if kind == "draw":
            await self._run_draw(user_id, data)
            self.store.finish_job(int(job["id"]), True)
            return

        try:
            prov = await self.gate.resolve(user_id)
        except NoProvider:
            # 客户 key 失效时，异步任务也不能偷偷跑
            self.store.finish_job(int(job["id"]), False, "no provider")
            return

        try:
            if kind == "facts":
                got = await F.refresh(
                    self.store,
                    self.session,
                    user_id,
                    line_id,
                    prov["api_base"],
                    prov["api_key"],
                    prov["model"],
                )
                if not got:
                    # 没抽出事实是正常的（这段对话里本来就没有个人信息），
                    # 但要留痕，避免以后排查时以为任务没跑。
                    self.store.finish_job(int(job["id"]), True, "no facts to extract")
                    return
            elif kind == "summary":
                await S.maybe_update(
                    self.store,
                    self.session,
                    user_id,
                    line_id,
                    prov["api_base"],
                    prov["api_key"],
                    prov["model"],
                )
            self.store.finish_job(int(job["id"]), True)
        except Exception as e:
            self.store.finish_job(int(job["id"]), False, f"{type(e).__name__}: {e}")
