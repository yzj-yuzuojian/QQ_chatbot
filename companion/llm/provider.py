"""客户 provider 解析 + 三层拦截。

这是资金安全的底线。见方案文档 §2.1。

设计原则：
    找不到可用的客户 provider 时，**必须显式拒绝**。
    绝不能出现"找不到就用默认"的兜底——那会静默消耗站长的额度。
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from .. import text as T
from ..db.store import Store

# 校验缓存时长：太短会频繁打接口，太长感知不到失效
CHECK_TTL = 600  # 10 分钟


class NoProvider(Exception):
    """没有可用的 provider。调用方必须拦截，不能继续走模型。"""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message


@dataclass
class KeyInfo:
    valid: bool
    balance: int = 0        # quota 单位，500000 = $1
    used: int = 0
    unlimited: bool = False
    models: list[str] | None = None        # token 的 model_limits（白名单）
    available: list[str] | None = None     # /v1/models 返回的**该 key 实际可用**模型
    expires_at: int = 0
    error: str | None = None
    # True 表示"没查出来，但也不能判定失效"（429/5xx/超时）。
    # 这种情况必须放行——因为 /api/usage/token/ 只是额度查询，
    # 它挂了不代表聊天接口不能用。实测：限流时它 429，聊天接口照样 200。
    unknown: bool = False

    def balance_cny(self, rate: float = 7.2) -> float:
        """quota → 人民币。quota_per_unit = 500000 表示 500000 quota = $1。"""
        return self.balance / 500000.0 * rate


async def check_key(
    session: aiohttp.ClientSession, api_base: str, key: str
) -> KeyInfo:
    """校验 key 并取余额。

    接口：GET {api_base去掉/v1}/api/usage/token/
    已实测：
        有效   → 200 {"code":true,"data":{...}}
        无效   → 401 {"message":"Invalid token","success":false}
        无 header → 401 {"message":"Token not provided"}
    """
    root = api_base.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    url = f"{root}/api/usage/token/"

    try:
        async with session.get(
            url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status == 401:
                return KeyInfo(valid=False, error="key 无效或已被删除")

            # 429 是"太频繁"，不是 key 有问题。
            # 必须区分开：如果把 429 当失效，客户会以为 key 坏了，
            # 跑去重新生成 key，问题反而更严重。
            if r.status == 429:
                # 额度接口挂了，但 /v1/models 可能是好的——
                # 它们是两个接口，限流/故障互不相干。
                # 实测：/api/usage/token/ 429 时 /v1/models 照样 200。
                models = await list_models(session, root, key)
                return KeyInfo(
                    valid=False,
                    unknown=True,
                    available=models,
                    error="额度查询被限流（不影响聊天）",
                )

            # 5xx 是上游故障，同样不能判 key 死刑
            if r.status >= 500:
                models = await list_models(session, root, key)
                return KeyInfo(
                    valid=False,
                    unknown=True,
                    available=models,
                    error=f"额度查询失败（HTTP {r.status}），不影响聊天",
                )

            if r.status != 200:
                return KeyInfo(valid=False, error=f"校验失败 HTTP {r.status}")

            body = await r.json()
            data = body.get("data") or {}

            models: list[str] | None = None
            ml = data.get("model_limits") or {}
            if data.get("model_limits_enabled") and ml:
                models = sorted(ml.keys())

            # 拉该 key 实际可用的模型列表。
            # 这一步很关键：客户 key 属于哪个分组，只能用那个分组的模型。
            # 写死一份默认列表会让 gemini 分组的客户看到 deepseek 模型，选了也用不了。
            available = await list_models(session, root, key)

            return KeyInfo(
                valid=True,
                balance=int(data.get("total_available") or 0),
                used=int(data.get("total_used") or 0),
                unlimited=bool(data.get("unlimited_quota")),
                models=models,
                available=available,
                expires_at=int(data.get("expires_at") or 0),
            )
    except asyncio.TimeoutError:
        return KeyInfo(valid=False, unknown=True, error="额度查询超时（不影响聊天）")
    except Exception as e:
        return KeyInfo(
            valid=False, unknown=True, error=f"额度查询异常：{type(e).__name__}"
        )


# 便宜的模型排前面：陪伴场景对话量大，默认该选便宜的
_CHEAP_HINTS = ("flash", "lite", "mini", "haiku", "small", "turbo", "air")
_DEAR_HINTS = ("pro", "opus", "sonnet", "max", "ultra", "thinking", "reason")


def _sort_models(names: list[str]) -> list[str]:
    """模型排序：便宜的在前，贵的在后。

    为什么不能纯字母序：客户发 /model 看到的第一屏应该是"日常聊天用这个"，
    而不是先看到 gemini-2.5-pro 这种贵的。
    """

    def rank(m: str) -> tuple[int, str]:
        low = m.lower()
        cheap = any(h in low for h in _CHEAP_HINTS)
        dear = any(h in low for h in _DEAR_HINTS)
        if cheap and not dear:
            return (0, low)
        if dear and not cheap:
            return (2, low)
        return (1, low)

    return sorted(names, key=rank)


async def list_models(
    session: aiohttp.ClientSession, api_base: str, key: str
) -> list[str] | None:
    """取这个 key **实际可用**的模型。

    来源：GET {root}/v1/models
    New API 的行为（已核对 controller/model.go ListModels）：
        用 token 的 group 过滤，所以 gemini 分组的 token 只会看到 gemini 模型。
    这是唯一正确的来源——不能写死列表，也不能用 /api/pricing（那是全站模型）。

    返回 None 表示拉取失败（上层退回"让客户手输模型名"）。
    """
    root = api_base.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    url = f"{root}/v1/models"

    try:
        async with session.get(
            url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status != 200:
                return None
            body = await r.json()

        items = body.get("data") or []
        names = [
            str(m.get("id"))
            for m in items
            if isinstance(m, dict) and m.get("id")
        ]
        if not names:
            return None
        return _sort_models(names)
    except Exception:
        return None


class ProviderGate:
    """三层拦截的唯一实现点。

    任何要调用模型的地方都必须先过这里。
    """

    def __init__(
        self,
        store: Store,
        crypto: Any,
        session: aiohttp.ClientSession,
        log: Any = print,
    ) -> None:
        self.store = store
        self.crypto = crypto
        self.session = session
        self.log = log

    # ------------------------------------------------------------------
    # 第 ② 层：请求前校验
    # ------------------------------------------------------------------

    async def resolve(self, user_id: int) -> dict[str, Any]:
        """拿到可用的 provider 配置，或抛 NoProvider。

        返回：{"api_base", "api_key", "model", "persona_id"}
        """
        cfg = self.store.get_config(user_id)
        if not cfg:
            raise NoProvider("no_config", "配置缺失")

        cipher = cfg.get("key_cipher")
        if not cipher:
            raise NoProvider("no_key", "尚未绑定 key")

        api_base = cfg.get("api_base") or ""
        model = cfg.get("model") or ""
        if not api_base:
            raise NoProvider("no_base", "未设置接口地址")
        if not model:
            raise NoProvider("no_model", "尚未选择模型")

        # 解密
        try:
            api_key = self.crypto.decrypt(cipher)
        except Exception:
            raise NoProvider("decrypt_failed", "密钥解密失败，请重新绑定")

        # 校验（带缓存）
        checked_at = int(cfg.get("key_checked_at") or 0)
        fresh = (time.time() - checked_at) < CHECK_TTL

        # 缓存可信的前提：上次是真的查到了额度（last_error 为空）。
        # 如果上次是"额度未知"（429/超时），缓存里的 balance 不可信——
        # 尤其不能因为缓存里 balance=0 就判客户额度用完，
        # 那会在中转站限流期间把所有人挡在门外。
        cache_trustworthy = fresh and cfg.get("key_valid") and not cfg.get("key_last_error")

        if cache_trustworthy:
            if not cfg.get("key_unlimited") and int(cfg.get("key_balance") or 0) <= 0:
                raise NoProvider("no_balance", "额度已用尽，请充值")
        else:
            info = await check_key(self.session, api_base, api_key)

            if info.unknown:
                # 查不到额度 ≠ key 失效。
                # 放行，同时把"这次没查到"记进缓存（last_error 非空），
                # 这样接下来 10 分钟内都会走重新查询，而不是拿旧余额判死。
                self.store.update_check(
                    user_id,
                    valid=bool(cfg.get("key_valid")),
                    balance=int(cfg.get("key_balance") or 0),
                    unlimited=bool(cfg.get("key_unlimited")),
                    error=info.error,
                    available_models=info.available,
                )
                self.log(f"[provider] user={user_id} 额度未知，放行：{info.error}")
            else:
                self.store.update_check(
                    user_id,
                    valid=info.valid,
                    balance=info.balance,
                    unlimited=info.unlimited,
                    error=info.error,
                    available_models=info.available,
                )
                if not info.valid:
                    raise NoProvider("invalid_key", info.error or "key 校验失败")
                if not info.unlimited and info.balance <= 0:
                    raise NoProvider("no_balance", "额度已用尽，请充值")

        return {
            "api_base": api_base,
            "api_key": api_key,
            "model": model,
            "persona_id": cfg.get("persona_id") or "default",
        }

    # ------------------------------------------------------------------
    # 第 ③ 层：启动断言
    # ------------------------------------------------------------------

    @staticmethod
    def assert_no_default_provider() -> None:
        """启动时调用。

        断言没有任何"平台默认 provider"可用。
        如果有，说明配置里有站长自己的 key，异常路径会用它花钱。
        """
        env_key = os.environ.get("PLATFORM_LLM_KEY") or os.environ.get("OPENAI_API_KEY")
        if env_key:
            raise RuntimeError(
                "检测到平台级 LLM key 环境变量。这会成为兜底路径，"
                "导致客户 key 失效时静默消耗站长额度。请移除 PLATFORM_LLM_KEY / OPENAI_API_KEY。"
            )


# ---------------------------------------------------------------------------
# 解密
# ---------------------------------------------------------------------------


class Crypto:
    """Fernet 加解密。

    密钥从环境变量注入，不落代码库。
    未配置时降级为"明文存储 + 启动告警"——开发环境方便，生产必须配。
    """

    def __init__(self, token: str | None = None) -> None:
        self.token = token or os.environ.get("COMPANION_SECRET", "")
        self._fernet = None
        if self.token:
            try:
                from cryptography.fernet import Fernet

                self._fernet = Fernet(self.token.encode())
            except Exception as e:
                raise RuntimeError(f"COMPANION_SECRET 无效：{e}")

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def encrypt(self, plain: str) -> str:
        if not self._fernet:
            return "plain:" + plain
        return self._fernet.encrypt(plain.encode()).decode()

    def decrypt(self, cipher: str) -> str:
        if cipher.startswith("plain:"):
            return cipher[6:]
        if not self._fernet:
            raise RuntimeError("缺少 COMPANION_SECRET，无法解密")
        return self._fernet.decrypt(cipher.encode()).decode()

    @staticmethod
    def generate_secret() -> str:
        from cryptography.fernet import Fernet

        return Fernet.generate_key().decode()
