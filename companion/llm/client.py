"""OpenAI 兼容的模型调用。

只做一件事：把消息发出去，把回复拿回来。
不做重试风暴（失败一次就让上层处理），不做流式（QQ 不支持流式输出）。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import aiohttp


@dataclass
class Completion:
    content: str
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""


class LLMError(Exception):
    pass


# 这些模型不认 reasoning_effort，发过去会 400。
# 目前实测 gemini / gpt / claude 系都认，先留空。
_NO_REASONING_UNSUPPORTED: set[str] = set()


async def chat(
    session: aiohttp.ClientSession,
    api_base: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.8,
    max_tokens: int = 800,
    no_thinking: bool = True,
    timeout: int = 120,
) -> Completion:
    """调用 chat completions。

    temperature 默认 0.8：陪伴场景需要一点随机性，
    太低像客服，太高人设会崩。实测 0.7–0.85 最好。
    """
    url = api_base.rstrip("/")
    if not url.endswith("/chat/completions"):
        url = url + "/chat/completions"

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }

    # 关掉推理模型的思考过程。
    #
    # 实测 gemini-3.8-flash-high：一次"说句简短的话"，返回 428 token，
    # 其中 419 是 reasoning，真正的内容只有 10 个字，耗时 5.9 秒。
    # 加 reasoning_effort=none 后：2.4 秒，reasoning 归零。
    #
    # 陪伴聊天不需要"思考"——它在想"这句话该怎么回"的时间，
    # 客户就是在干等。而且思考 token 算客户的钱。
    # 如果某个模型不认这个参数，加进 _NO_REASONING_UNSUPPORTED 就跳过。
    if no_thinking and model not in _NO_REASONING_UNSUPPORTED:
        payload["reasoning_effort"] = "none"

    try:
        async with session.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as r:
            body = await r.text()
            if r.status == 401:
                raise LLMError("key 无效或已过期")
            if r.status == 402:
                raise LLMError("额度不足")
            if r.status == 429:
                raise LLMError("请求太频繁，请稍后再试")
            # 400 且是 reasoning_effort 引起的 → 去掉参数重试一次。
            # 不同模型/中转站对这个字段的支持不一致，直接报错会让客户
            # 一换模型就"用不了"，且完全看不出原因。
            if r.status == 400 and "reasoning_effort" in payload:
                _NO_REASONING_UNSUPPORTED.add(model)
                payload.pop("reasoning_effort", None)
                async with session.post(
                    url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as r2:
                    body2 = await r2.text()
                    if r2.status < 400:
                        try:
                            data = await r2.json()
                        except Exception:
                            raise LLMError(f"响应解析失败：{body2[:120]}")
                        choices = data.get("choices") or []
                        if not choices:
                            raise LLMError("模型返回空结果")
                        usage = data.get("usage") or {}
                        return Completion(
                            content=(
                                (choices[0].get("message") or {}).get("content") or ""
                            ).strip(),
                            tokens_in=int(usage.get("prompt_tokens") or 0),
                            tokens_out=int(usage.get("completion_tokens") or 0),
                            model=data.get("model") or model,
                        )
                    body = body2
                    r_status = r2.status
                raise LLMError(f"模型返回 {r_status}：{body[:120]}")

            if r.status >= 400:
                raise LLMError(f"模型返回 {r.status}：{body[:120]}")

            try:
                data = await r.json()
            except Exception:
                raise LLMError(f"响应解析失败：{body[:120]}")

    except asyncio.TimeoutError:
        raise LLMError("模型响应超时")
    except aiohttp.ClientError as e:
        raise LLMError(f"网络错误：{type(e).__name__}")

    choices = data.get("choices") or []
    if not choices:
        raise LLMError("模型返回空结果")

    content = (choices[0].get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}

    return Completion(
        content=content.strip(),
        tokens_in=int(usage.get("prompt_tokens") or 0),
        tokens_out=int(usage.get("completion_tokens") or 0),
        model=data.get("model") or model,
    )
