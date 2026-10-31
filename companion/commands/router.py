"""命令路由。

命令分三类（见方案文档 §2.1 边界情况表）：
    ① 放行 —— 不管有没有配 key 都要能用（/start /setkey /help /forget）
    ② 需配置 —— 配完才能用（/model /persona /stats）
    ③ 普通消息 —— 必须配好才能走对话
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .. import text as T
from ..db.store import Store
from ..llm.provider import NoProvider, ProviderGate, check_key
from ..persona import cards as C
from .settings import (
    COSTLY_COMMANDS,
    MAX_INPUT_CHARS,
    PERSONA_RESERVED,
    RATE_DOUBLE_TAP,
    RATE_MAX_CALLS,
    RATE_WINDOW,
)
from .texts import (
    CONSENT_TEXT,
    HELP_TEXT,
    NOT_CONFIGURED,
    PRIVACY_TEXT,
    START_TEXT,
)

# 不需要 key 就能用的命令
FREE_COMMANDS = {"/start", "/setkey", "/help", "/forget", "/delkey"}

def _derive_site(api_base: str) -> str:
    """从 api_base 推站点根地址。api_base 通常以 /v1 结尾。"""
    base = (api_base or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base or "你的中转站"


def _closest(target: str, options: list[str]) -> str:
    """在候选里找最像的一个，用于"你是不是想用 X"提示。"""
    if not options:
        return ""
    t = target.lower()

    # 先看包含关系
    for o in options:
        if t and (t in o.lower() or o.lower() in t):
            return o

    # 再看前缀
    for o in options:
        if t[:4] and o.lower().startswith(t[:4]):
            return o
    return ""


def _render(template: str, site: str) -> str:
    """把 {site} 换成实际地址。文案里没占位符就原样返回。"""
    if "{site}" not in template:
        return template
    return template.replace("{site}", site)


@dataclass
class Command:
    name: str
    args: list[str]


def parse(text: str) -> Command | None:
    """解析命令。只认 `/` 开头的，且不含空格的长文本。"""
    t = text.strip()
    if not t.startswith("/"):
        return None
    parts = t.split()
    if not parts:
        return None
    # 防止把 "/ 今天我..." 这种当命令：
    # 命令名至少 2 个字符，且只含字母数字下划线
    name = parts[0].lower()
    if len(name) < 2 or len(name) > 20:
        return None
    if not name[1:].replace("_", "").isalnum():
        return None
    return Command(name=name, args=parts[1:])


class Router:
    def __init__(
        self,
        store: Store,
        gate: ProviderGate,
        engine: Any,
        session: Any,
        crypto: Any,
        api_base: str,
        bind_url: str,
        log: Any = print,
        site_url: str = "",
    ) -> None:
        self.store = store
        self.gate = gate
        self.engine = engine
        self.session = session
        self.crypto = crypto
        self.api_base = api_base
        self.bind_url = bind_url
        self.log = log
        # 客户看到"去哪申请 key"用的地址。
        # 开源后别人的中转站不同，不能写死。
        self.site_url = site_url or _derive_site(api_base)
        # 限流：user_id -> 最近消息时间戳列表
        self._rate: dict[int, list[float]] = {}

    def _t(self, template: str) -> str:
        """渲染文案里的占位符。所有面向客户的文案都该走这里。"""
        return _render(template, self.site_url)

    def _check_rate(self, user_id: int, costly: bool = True) -> str | None:
        """限流检查。返回提示语表示被限，None 表示放行。

        Args:
            costly: 这次操作是否"要花钱或打外部接口"。
                    命令层会先判断，纯本地命令传 False 直接放行。

        限的是两类东西：
            ① 普通对话 —— 每次调模型都烧客户自己的 key
            ② /model /setkey —— 每次都打中转站接口，
               连发会被上游限流，严重时封 IP

        不限的：
            /help /stats /persona /mood /privacy /forget /export /delkey
            这些全是本地操作，没有理由拦。
        """
        import time as _t

        if not costly:
            return None

        now = _t.time()
        hist = [
            t for t in self._rate.get(user_id, []) if now - t < RATE_WINDOW
        ]

        # ① 窗口内总量超限
        if len(hist) >= RATE_MAX_CALLS:
            wait = int(RATE_WINDOW - (now - hist[0])) + 1
            self._rate[user_id] = hist
            return (
                f"你发得太快了，等 {wait} 秒。\n"
                f"（每分钟最多 {RATE_MAX_CALLS} 次对话，用你自己的额度）"
            )

        # ② 极短间隔视为误触，静默丢弃。
        #    必须照常计入历史——否则连点 100 次会永远被静默、
        #    永远累积不到上限，限流等于失效。
        hist.append(now)
        self._rate[user_id] = hist

        if len(hist) >= 2 and (now - hist[-2]) < RATE_DOUBLE_TAP:
            return "__SILENT__"

        return None

    async def handle(
        self,
        user_id: int,
        text: str,
        chat_type: str,
        appid: str,
        target: dict | None = None,
        openid: str = "",
        msg_id: str = "",
    ) -> tuple[str, bool]:
        """处理一条消息。

        返回 (要回复的文本, 是否已处理)。
        第二个返回值用于区分"命令处理完了"和"需要走对话"。
        """
        # 消息太长：不静默丢弃，明确告诉客户。
        # 静默丢弃最糟——客户以为机器人坏了。
        if len(text) > MAX_INPUT_CHARS:
            return (
                f"这条消息太长了（{len(text)} 字），我一次最多看 "
                f"{MAX_INPUT_CHARS} 字。\n\n"
                "拆成几条发，或者只说重点。"
            ), True

        cmd = parse(text)

        # 限流：只拦"花钱/打接口"的。
        # 纯本地命令先放行，避免客户调设置时被限流挡住。
        costly = True
        if cmd:
            costly = cmd.name in COSTLY_COMMANDS
            # 没绑 key 的客户，配置命令只会打本地库（/model /setkey 提前返回），
            # 不会真的打中转站。这种不该限流——否则客户连"还没绑定 key"
            # 这句提示都会被静默吞掉，表现成"已读不回"。
            if costly and not self.store.get_config(user_id).get("key_cipher"):
                costly = False

        limited = self._check_rate(user_id, costly=costly)
        if limited:
            if limited == "__SILENT__":
                return "", True      # 空字符串 = 不回复
            return limited, True

        # ---- 命令 ----
        if cmd:
            name = cmd.name
            if name == "/help":
                return HELP_TEXT, True
            if name == "/start":
                return CONSENT_TEXT + self._t(START_TEXT), True
            if name == "/setkey":
                return await self._cmd_setkey(user_id, chat_type), True
            if name == "/delkey":
                self.store.clear_key(user_id)
                has_img = bool(self.store.get_config(user_id).get("image_cipher"))
                tail = "生图 key 还在。" if has_img else ""
                return f"已解绑聊天 key，记忆还在。{tail}", True
            if name in ("/setimage", "/image"):
                return await self._cmd_setimage(user_id, chat_type), True
            if name == "/delimage":
                self.store.clear_image_key(user_id)
                return "已解绑生图 key。聊天不受影响。", True
            if name == "/model":
                return await self._cmd_model(user_id, cmd.args), True
            if name == "/persona":
                return self._cmd_persona(user_id, cmd.args), True
            if name in ("/mood", "/tone"):
                return self._cmd_mood(user_id, cmd.args), True
            if name in ("/facts", "/memory"):
                return self._cmd_facts(user_id, cmd.args), True
            if name in ("/privacy", "/data"):
                return PRIVACY_TEXT, True
            if name in ("/export", "/download"):
                return self._cmd_export(user_id, chat_type), True
            if name == "/stats":
                return self._cmd_stats(user_id), True
            if name == "/forget":
                self.store.delete_user_messages(user_id)
                n = len(self.store.list_personas(user_id))
                msg = "已删除全部对话记录和记忆（包括事实卡）。"
                if n:
                    msg += (
                        f"\n\n你自己写的 {n} 个人设还留着——"
                        "那是你写的东西，不算记忆。\n"
                        "想一起删掉，发 /persona 然后逐个 /persona del 序号。"
                    )
                return msg, True

            if name == "/bind":
                return "已改用网页绑定：请发送 /setkey 获取链接。", True

            return f"没有 {name} 这个命令。发 /help 看看能做什么。", True

        # ---- 普通消息：必须已配置 ----
        return await self._chat(user_id, text, chat_type, target, openid, msg_id)

    # ------------------------------------------------------------------
    # /setkey
    # ------------------------------------------------------------------

    async def _cmd_setkey(self, user_id: int, chat_type: str) -> str:
        if chat_type != "c2c":
            return "请在私聊里配置。群里发链接不合适。"

        code = self.store.create_bind_code(user_id, ttl=600)
        url = f"{self.bind_url}?code={code}"
        return (
            "打开这个链接绑定你的 API Key（10 分钟内有效）：\n"
            f"{url}\n\n"
            "为什么不直接在聊天里发 key：QQ 聊天记录会永久留存，\n"
            "等于把密钥抄送给第三方。网页绑定更安全。"
        )

    # ------------------------------------------------------------------
    # /setimage —— 生图 key（和聊天 key 分开）
    # ------------------------------------------------------------------

    async def _cmd_setimage(self, user_id: int, chat_type: str) -> str:
        if chat_type != "c2c":
            return "请在私聊里配置。群里发链接不合适。"

        cfg = self.store.get_config(user_id)
        code = self.store.create_bind_code(user_id, ttl=600, kind="image")
        url = f"{self.bind_url}?code={code}"

        head = ""
        if cfg.get("image_cipher"):
            head = f"当前生图 key：****{cfg.get('image_suffix') or ''}\n换一个就点下面的链接。\n\n"

        return (
            f"{head}"
            "生图要单独配一个 key（和聊天 key 分开）。\n"
            f"{url}\n\n"
            "生图是按张收费的，比聊天贵不少，所以单独算。\n"
            "这个 key 要能调 gpt-image 系列模型。\n"
            "不支持的话，发图时会告诉你是分组没权限。\n\n"
            "配好之后直接说「画一只猫」就行。\n"
            "不想要了发 /delimage。"
        )

    # ------------------------------------------------------------------
    # /model
    # ------------------------------------------------------------------

    async def _cmd_model(self, user_id: int, args: list[str]) -> str:
        cfg = self.store.get_config(user_id)
        if not cfg.get("key_cipher"):
            return "还没绑定 key。先发 /setkey。"

        api_base = cfg.get("api_base") or self.api_base
        try:
            key = self.crypto.decrypt(cfg["key_cipher"])
        except Exception:
            return "密钥解密失败，请重新 /setkey。"

        info = await check_key(self.session, api_base, key)

        # 额度查询失败（429/5xx/超时）时不能判 key 死刑——
        # 这个接口只是查余额，它挂了不影响聊天。
        # 该用缓存里的模型列表继续，别让客户被卡在这里。
        if info.unknown:
            # 降级顺序：缓存列表 → 当前已选的模型 → 提示手输
            cached = self.store.get_available_models(user_id)
            current = cfg.get("model") or ""

            # 额度接口挂了，但 /v1/models 可能还活着 —— 优先用它
            if info.available:
                cached = info.available
                # 只缓存模型列表，**不能**把这次当成"校验成功"写进去。
                # 否则会写入 valid=1 / balance=0 / error=None，
                # 让缓存看起来可信，接下来 10 分钟 resolve 都判"额度用完"。
                self.store.update_check(
                    user_id,
                    valid=False,
                    balance=0,
                    unlimited=False,
                    error=info.error,        # 标记"这次没查到"
                    available_models=info.available,
                )

            if cached:
                candidates = cached
                notice = f"（额度暂时查不到：{info.error}）\n\n"
            elif current:
                # 已经选好模型的话，没必要拦——直接用当前模型继续
                candidates = [current]
                notice = (
                    f"（额度暂时查不到：{info.error}）\n"
                    f"继续用当前模型：{current}\n\n"
                )
            else:
                return (
                    f"暂时查不到可用模型：{info.error}\n\n"
                    "稍等一两分钟重试，或直接发完整模型名：\n"
                    "  /model 模型名"
                )
        elif not info.valid:
            return f"key 校验失败：{info.error}\n请重新 /setkey 或去后台检查。"
        else:
            self.store.update_check(
                user_id,
                valid=True,
                balance=info.balance,
                unlimited=info.unlimited,
                available_models=info.available,
            )
            notice = ""
            candidates = self._model_candidates(info, cfg)

        if not args:
            return notice + self._render_model_list(cfg, candidates, info)

        target = args[0].strip()

        if target.isdigit():
            idx = int(target) - 1
            if idx < 0 or idx >= len(candidates):
                return f"序号超出范围（1-{len(candidates)}）"
            model = candidates[idx]
        else:
            model = target

        # 校验：模型必须在这个 key 的可用列表里
        if candidates and model not in candidates:
            msg = f"这个 key 不能用 {model}。"
            # 给个最接近的建议，避免客户面对一堆模型无从下手
            hint = _closest(model, candidates)
            if hint:
                msg += f"\n你是不是想用：{hint}"
            msg += f"\n\n发 /model 看完整列表"
            return msg

        self.store.update_config(user_id, model=model)
        return f"已切换到：{model}\n\n现在可以正常聊天了。"

    def _model_candidates(self, info, cfg: dict) -> list[str]:
        """确定这个 key 能用哪些模型。

        优先级（重要）：
            1. /v1/models 返回的可用列表 ← 唯一正确来源
            2. token 的 model_limits 白名单
            3. 上次缓存的列表
            4. 全部失败 → 空列表（让客户手输）

        **绝不使用写死的默认列表** —— 那会让 gemini 分组的客户
        看到 deepseek 模型，选了之后调用必然失败。
        """
        if info.available:
            return info.available
        if info.models:
            return sorted(info.models)
        cached = self.store.get_available_models(int(cfg["user_id"]))
        if cached:
            return cached
        return []

    def _render_model_list(self, cfg: dict, candidates: list[str], info) -> str:
        cur = cfg.get("model") or "（未选）"
        lines = [f"当前模型：{cur}", ""]

        if not candidates:
            lines.append("没能自动获取模型列表（可能是接口限制）。")
            lines.append("直接发送完整模型名即可，例如：")
            lines.append("  /model gemini-2.5-pro")
            return "\n".join(lines)

        lines.append(f"你这个 key 可以用的模型（{len(candidates)} 个）：")
        for i, m in enumerate(candidates[:20], 1):
            mark = " ← 当前" if m == cur else ""
            lines.append(f"{i}. {m}{mark}")
        if len(candidates) > 20:
            lines.append(f"… 还有 {len(candidates)-20} 个，发 /model 模型名 直接用")

        lines.append("")
        lines.append("发 /model 序号 切换")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # /persona —— 人设（可以存多套，随时切换）
    # ------------------------------------------------------------------

    def _cmd_persona(self, user_id: int, args: list[str]) -> str:
        mod = self.store.get_config(user_id).get("modifier_id") or "normal"
        people = self.store.list_personas(user_id)
        active = self.store.active_persona(user_id)

        # ---- /persona（无参数）：列表 ----
        if not args:
            if not people:
                return (
                    "你还没有人设。\n\n"
                    "人设是让机器人扮演一个角色，比如：\n"
                    "/persona new 雪球 你是一只高冷的布偶猫，说话很短\n"
                    "/persona new 老周 你是我发小，说话很随意，会损我\n\n"
                    "可以存多套，随时切换。\n"
                    "不知道怎么写的，发 /persona example"
                )

            lines = ["【你的人设】 可以存多套，随时切换"]
            for i, p in enumerate(people, 1):
                mark = " ←" if active and p["id"] == active["id"] else ""
                lines.append(f"{i}. {p['name']}{mark}")
                # 显示前 40 字预览
                preview = p["content"].replace("\n", " ")[:40]
                lines.append(f"   {preview}{'…' if len(p['content']) > 40 else ''}")

            lines.append("")
            if active:
                lines.append(f"当前：{active['name']}")
            else:
                lines.append("当前：未指定（用默认陪聊风格）")
            if mod != "normal":
                lines.append(f"语气：{C.get_modifier(mod).name}（/mood 改）")
            lines.append("")
            lines.append("/persona 2            切到第 2 个")
            lines.append("/persona 雪球         按名字切")
            lines.append("/persona new 名字 描述  新建一个")
            lines.append("/persona edit 2 新描述  改第 2 个")
            lines.append("/persona del 2        删第 2 个")
            lines.append("/persona off          不用人设")
            return "\n".join(lines)

        sub = args[0].lower()

        # ---- /persona example ----
        if sub in ("example", "examples", "demo", "示例"):
            return C.examples()

        # ---- /persona off ----
        if sub in ("off", "clear", "reset", "默认"):
            if not active:
                return "你现在就是默认陪聊风格。"
            self.store.set_active_persona(user_id, None)
            return "已关闭人设，回到默认陪聊风格。\n\n人设都还在，/persona 可以随时切回来。"

        # ---- /persona new <名字> <描述> ----
        if sub in ("new", "add", "新建"):
            if len(args) < 3:
                return (
                    "格式：/persona new 名字 描述\n\n"
                    "例如：\n"
                    "/persona new 雪球 你是一只高冷的布偶猫，说话很短，偶尔不理人\n"
                    "/persona new 老周 你是我发小，做设计的，说话随意，会损我但不会伤人"
                )
            name = args[1].strip()
            desc = " ".join(args[2:]).strip()

            # 名字里有保留字会导致永远切不到它（例如建了个叫 new 的人设，
            # 发 /persona new 永远被当成子命令）。
            if name.lower() in PERSONA_RESERVED:
                return (
                    f"「{name}」是保留字，不能当人设名。\n"
                    "换个名字，比如：/persona new 雪球 你的描述"
                )

            if len(name) > 12:
                return f"名字太长了（{len(name)} 字），最多 12 字。"

            if len(people) >= C.MAX_PERSONAS:
                return (
                    f"最多存 {C.MAX_PERSONAS} 个人设，你已经满了。\n\n"
                    "想加新的，先删一个：\n"
                    "  /persona          看列表\n"
                    "  /persona del 序号  删掉不用的\n\n"
                    "或者改现有的：/persona edit 序号 新描述"
                )

            if self.store.find_persona_by_name(user_id, name):
                return f"已经有叫「{name}」的人设了。\n换个名字，或用 /persona edit 改它。"

            ok, msg = C.validate_persona(desc)
            if not ok:
                return msg

            pid = self.store.add_persona(user_id, name, desc)
            self.store.set_active_persona(user_id, pid)
            return (
                f"已创建「{name}」并切换过去。\n\n"
                f"「{desc[:100]}{'…' if len(desc) > 100 else ''}」\n\n"
                "现在开始就按这个说话了。\n"
                "跟其他人设的聊天不会混，但它还是记得你的事。"
            )

        # ---- /persona edit <序号|名字> <新描述> ----
        if sub in ("edit", "set", "改"):
            if len(args) < 3:
                # /persona set <描述> 兼容旧用法：改当前人设
                if sub == "set" and len(args) >= 2:
                    desc = " ".join(args[1:]).strip()
                    if not active:
                        return (
                            "你还没有人设。\n\n"
                            "新建：/persona new 名字 描述\n"
                            "例如：/persona new 雪球 你是一只高冷的布偶猫"
                        )
                    ok, msg = C.validate_persona(desc)
                    if not ok:
                        return msg
                    self.store.update_persona(user_id, active["id"], active["name"], desc)
                    return f"已更新「{active['name']}」。"
                return (
                    "格式：/persona edit 序号 新描述\n"
                    "例如：/persona edit 2 你是我姐姐，会管我但不会训我"
                )

            target = self._resolve_persona(user_id, args[1], people)
            if not target:
                return f"找不到「{args[1]}」。发 /persona 看列表。"

            desc = " ".join(args[2:]).strip()
            ok, msg = C.validate_persona(desc)
            if not ok:
                return msg

            self.store.update_persona(user_id, target["id"], target["name"], desc)
            return f"已更新「{target['name']}」。"

        # ---- /persona rename <序号|名字> <新名字> ----
        if sub in ("rename", "改名"):
            if len(args) < 3:
                return "格式：/persona rename 2 新名字\n发 /persona 看列表。"
            target = self._resolve_persona(user_id, args[1], people)
            if not target:
                return f"找不到「{args[1]}」。发 /persona 看列表。"
            new_name = args[2].strip()
            if new_name.lower() in PERSONA_RESERVED:
                return f"「{new_name}」是保留字，换一个。"
            if len(new_name) > 12:
                return f"名字太长了，最多 12 字。"
            if not self.store.rename_persona(user_id, target["id"], new_name):
                return f"已经有叫「{new_name}」的人设了。"
            return f"已改名：{target['name']} → {new_name}"

        # ---- /persona del <序号|名字> ----
        if sub in ("del", "delete", "rm", "删"):
            if len(args) < 2:
                return "格式：/persona del 序号\n发 /persona 看列表。"
            target = self._resolve_persona(user_id, args[1], people)
            if not target:
                return f"找不到「{args[1]}」。发 /persona 看列表。"

            # 名字对不上就不删（序号会被列表顺序变化影响，容易删错）
            n_msg = self.store.count_messages(user_id, f"p{target['id']}")
            if len(args) < 3 or args[2] != target["name"]:
                lines = [
                    f"确认删除「{target['name']}」？",
                    "",
                    f"这条对话线有 {n_msg} 条消息，删了就找不回来了。",
                ]
                if active and active["id"] == target["id"]:
                    lines.append("（它正在使用中，删掉后会自动回到默认风格）")
                lines.append("")
                lines.append(f"确认请发：/persona del {args[1]} {target['name']}")
                lines.append("不想删就发别的，什么都不会变。")
                return "\n".join(lines)

            self.store.delete_persona(user_id, target["id"])
            # 如果删的是当前激活的，自动回默认
            if active and active["id"] == target["id"]:
                self.store.set_active_persona(user_id, None)
                return (
                    f"已删除「{target['name']}」和它的 {n_msg} 条消息。\n"
                    "现在回到默认陪聊风格。"
                )
            return f"已删除「{target['name']}」和它的 {n_msg} 条消息。"

        # ---- /persona <序号|名字>：切换 ----
        target = self._resolve_persona(user_id, args[0], people)
        if not target:
            return (
                f"找不到「{args[0]}」。\n"
                "发 /persona 看列表，或 /persona new 名字 描述 新建。"
            )

        self.store.set_active_persona(user_id, target["id"])
        lines = [f"已切换到「{target['name']}」", ""]
        lines.append(f"「{target['content'][:80]}{'…' if len(target['content']) > 80 else ''}」")
        lines.append("")
        if mod != "normal":
            m = C.get_modifier(mod)
            lines.append(f"当前叠加语气：{m.name}（/mood 改）")
        lines.append("这里的聊天跟其他人设分开，但你的事它都记得。")
        return "\n".join(lines)

    def _resolve_persona(
        self, user_id: int, token: str, people: list[dict]
    ) -> dict | None:
        """按序号或名字找人设。"""
        token = (token or "").strip()
        if not token:
            return None
        if token.isdigit():
            idx = int(token) - 1
            if 0 <= idx < len(people):
                return people[idx]
            return None
        for p in people:
            if p["name"] == token:
                return p
        return None

    # ------------------------------------------------------------------
    # /mood —— 语气（只改说法，不改人设）
    # ------------------------------------------------------------------

    def _cmd_mood(self, user_id: int, args: list[str]) -> str:
        cfg = self.store.get_config(user_id)
        cur = cfg.get("modifier_id") or "normal"

        if not args:
            lines = [f"当前语气：{C.get_modifier(cur).name}", ""]
            for i, m in enumerate(C.list_modifiers(), 1):
                mark = " ←" if m.id == cur else ""
                lines.append(f"{i}. {m.name}{mark}")
                if m.prompt:
                    lines.append(f"   {m.prompt}")
            lines.append("")
            lines.append("发 /mood 序号 或 /mood 名字 切换")
            lines.append("语气只改说法方式，不动人设。")
            return "\n".join(lines)

        mid = C.find_modifier(args[0])
        if not mid:
            return f"没有「{args[0]}」这个语气。发 /mood 看列表。"

        self.store.update_config(user_id, modifier_id=mid)
        m = C.get_modifier(mid)
        out = f"已切换语气：{m.name}"
        if m.prompt:
            out += f"\n（{m.prompt}）"
        return out

    # ------------------------------------------------------------------
    # /facts —— 事实卡（它记着关于你的事）
    # ------------------------------------------------------------------

    def _cmd_facts(self, user_id: int, args: list[str]) -> str:
        facts = self.store.get_facts(user_id, "")

        # /facts（无参数）—— 看
        if not args:
            if not facts:
                return (
                    "我还不知道关于你的事。\n\n"
                    "跟我聊几轮，我会自己记一些。\n"
                    "也可以直接告诉我：\n"
                    "  /facts set 称呼 林晚\n"
                    "  /facts set 宠物 两只猫：豆豆、花卷\n"
                    "  /facts set 饮食忌口 对芒果过敏"
                )

            lines = ["【它记得关于你的事】"]
            manual = [f for f in facts if f["source"] == "manual"]
            auto = [f for f in facts if f["source"] != "manual"]

            if manual:
                lines.append("")
                lines.append("你写的：")
                for f in manual:
                    lines.append(f"  {f['key']}：{f['value']}")
            if auto:
                lines.append("")
                lines.append("它自己记的：")
                for f in auto:
                    lines.append(f"  {f['key']}：{f['value']}")

            lines.append("")
            lines.append("这些在所有角色里共享。")
            lines.append("")
            lines.append("/facts set 类别 内容   加一条")
            lines.append("/facts del 类别        删一条")
            lines.append("/facts clear          清空（自己写的保留）")
            return "\n".join(lines)

        sub = args[0].lower()

        # /facts set <类别> <内容>
        if sub in ("set", "add", "设", "加"):
            if len(args) < 3:
                return (
                    "格式：/facts set 类别 内容\n\n"
                    "例子：\n"
                    "  /facts set 称呼 林晚\n"
                    "  /facts set 宠物 两只猫：豆豆、花卷\n"
                    "  /facts set 职业 在成都做插画师\n"
                    "  /facts set 饮食忌口 对芒果过敏"
                )
            key = args[1].strip()[:20]
            value = " ".join(args[2:]).strip()[:200]
            if not key or not value:
                return "类别和内容都不能空。"
            if len(value) < 1:
                return "内容太短了。"

            self.store.set_fact(user_id, key, value)
            return (
                f"记住了：{key} — {value}\n\n"
                "这条是你自己写的，不会被自动覆盖，所有角色都通用。"
            )

        # /facts del <类别>
        if sub in ("del", "delete", "rm", "删"):
            if len(args) < 2:
                lines = ["要删哪一条？发 /facts del 类别，例如：", ""]
                for f in facts:
                    lines.append(f"  /facts del {f['key']}")
                return "\n".join(lines) if facts else "现在没有记录。"

            key = args[1].strip()
            # 容忍模糊匹配：客户可能不记得确切类别名
            hit = next((f for f in facts if f["key"] == key), None)
            if not hit:
                hit = next((f for f in facts if key in f["key"]), None)
            if not hit:
                return f"没有「{key}」这条。发 /facts 看全部。"

            self.store.delete_fact(user_id, hit["key"])
            return f"已删除：{hit['key']} — {hit['value']}"

        # /facts clear
        if sub in ("clear", "reset", "清空"):
            n = self.store.clear_facts(user_id, keep_manual=True)
            if n == 0:
                return "没有可以清的（你自己写的会保留）。"
            return (
                f"已清掉 {n} 条自动记住的。\n"
                "你自己写的那几条还在。\n\n"
                "全清（含手写）需要发：/facts wipe"
            )

        # /facts wipe —— 连手写一起清
        if sub in ("wipe",):
            n = self.store.clear_facts(user_id, keep_manual=False)
            return f"已清空全部 {n} 条记忆。"

        return (
            f"没有「{args[0]}」这个用法。\n\n"
            "/facts              看全部\n"
            "/facts set 类别 内容  加一条\n"
            "/facts del 类别      删一条\n"
            "/facts clear         清自动记的\n"
            "/facts wipe          清全部"
        )

    # ------------------------------------------------------------------
    # /privacy —— 隐私说明
    # ------------------------------------------------------------------

    def _cmd_privacy(self, user_id: int, args: list[str]) -> str:
        return PRIVACY_TEXT

    # ------------------------------------------------------------------
    # /export —— 导出我的数据
    # ------------------------------------------------------------------

    def _cmd_export(self, user_id: int, chat_type: str) -> str:
        if chat_type != "c2c":
            return "导出数据请在私聊里操作。"

        cur = self.store.conn.execute(
            """SELECT role, content, created_at FROM messages
               WHERE user_id=? ORDER BY id""",
            (user_id,),
        )
        rows = cur.fetchall()
        if not rows:
            return "你还没有对话记录，没什么可导出的。"

        facts = self.store.get_facts(user_id, "")
        usage = self.store.get_usage(user_id)

        code = self.store.create_export_code(user_id)
        url = f"{self.bind_url.rsplit('/bind', 1)[0]}/export?code={code}"

        lines = [
            "【你的数据】",
            f"对话消息：{len(rows)} 条",
            f"记忆条目：{len(facts)} 条",
            f"对话轮次：{usage['turns']}",
            "",
            f"完整导出（JSON）：{url}",
            "",
            "链接 30 分钟内有效，只能用一次。",
            "删除全部数据：/forget",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # /stats
    # ------------------------------------------------------------------

    def _cmd_stats(self, user_id: int) -> str:
        cfg = self.store.get_config(user_id)
        usage = self.store.get_usage(user_id)
        total_msgs = self.store.count_messages(user_id)

        lines = ["【用量】"]
        lines.append(f"对话轮次：{usage['turns']}")
        lines.append(f"消息条数：{total_msgs}")

        if cfg.get("key_cipher"):
            fp = cfg.get("key_fingerprint") or ""
            suffix = cfg.get("key_suffix") or ""
            lines.append("")
            lines.append("【Key】")
            lines.append(f"sk-****{suffix}  指纹 {fp}")
            if cfg.get("key_unlimited"):
                lines.append("额度：无限")
            else:
                bal = int(cfg.get("key_balance") or 0)
                lines.append(f"余额：{bal / 500000 * 7.2:.2f} 元")
            if cfg.get("model"):
                lines.append(f"模型：{cfg['model']}")
            if cfg.get("key_last_error"):
                lines.append(f"⚠️ {cfg['key_last_error']}")
        else:
            lines.append("")
            lines.append("⚠️ 还没绑定 key，发 /setkey")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 普通对话
    # ------------------------------------------------------------------

    async def _chat(
        self,
        user_id: int,
        text: str,
        chat_type: str,
        target: dict | None = None,
        openid: str = "",
        msg_id: str = "",
    ) -> tuple[str, bool]:
        # 群聊不允许配置，提示私聊
        cfg = self.store.get_config(user_id)
        if not cfg.get("key_cipher"):
            if chat_type != "c2c":
                return "请先私聊我完成配置，然后就能在任何地方聊了。", True
            return self._t(NOT_CONFIGURED), True

        if not cfg.get("model"):
            return "还没选模型。发 /model 选一个。", True

        modifier_id = cfg.get("modifier_id") or "normal"

        # 每个人设一条独立对话线：换角色不串味，切回来上下文还在。
        # 事实卡是共享的（facts.persona_id=''）——换个角色也该记得你是谁的谁。
        active = self.store.active_persona(user_id)
        if active:
            custom_persona = active["content"]
            line_id = f"p{active['id']}"
        else:
            custom_persona = ""
            line_id = "main"

        try:
            answer, draw_prompt = await self.engine.reply(
                user_id,
                line_id,
                text,
                modifier_id=modifier_id,
                custom_persona=custom_persona,
            )
        except NoProvider as e:
            return self._provider_error(e), True
        except Exception as e:
            self.log(f"[chat] {type(e).__name__}: {e!r}")
            msg = str(e)
            if "失效" in msg or "无效" in msg:
                return "你的 key 好像失效了。发 /setkey 重新绑定。", True
            if "额度" in msg:
                return "额度用完了，去后台充值后再聊。", True
            if "超时" in msg:
                return "我这会儿有点慢，等一下再试试。", True
            return "出了点问题，稍后再试。", True

        # 模型判断要画图。
        # 没配生图 key 就明确说画不了——绝不偷偷用平台 key 花平台的钱，
        # 也不能假装画好了（那是撒谎，客户会发现被放鸽子）。
        if draw_prompt:
            cfg2 = self.store.get_config(user_id)
            if not cfg2.get("image_cipher"):
                answer = (
                    f"{answer}\n\n"
                    "（想画图得先配生图 key，发 /setimage。"
                    "聊天用的 key 不一定能生图，所以要单独配。）"
                )
            else:
                # 塞进队列，由 main 里的发送逻辑取走。
                # 用队列而不是改 handle 的返回值：handle 在很多地方被调，
                # 改签名会牵连一大片，而且命令分支根本不需要图片。
                self.store.enqueue(
                    "draw",
                    user_id,
                    {
                        "prompt": draw_prompt[:800],
                        "persona_id": line_id,
                        "target": target or {},
                        "openid": openid,
                        # 带上 msg_id，图片就能走被动回复（20 条配额）
                        # 而不是主动消息（每月可能只有几条）。
                        "msg_id": msg_id,
                    },
                )
                answer = f"{answer}\n\n（画着呢，等我一小会儿）"

        return answer, True

    def _provider_error(self, e: NoProvider) -> str:
        """把 NoProvider 转成可操作的话术。不能只说"出错了"。"""
        mapping = {
            "no_key": self._t(NOT_CONFIGURED),
            "no_model": "还没选模型。发 /model 选一个。",
            "no_base": "接口地址丢了，请重新 /setkey。",
            "invalid_key": "你的 key 失效了。发 /setkey 重新绑定。",
            "no_balance": "额度用完了，去后台充值后再聊。",
            "decrypt_failed": "密钥读取失败，请重新 /setkey。",
        }
        return mapping.get(e.reason, "配置有问题，发 /help 看看怎么设置。")
