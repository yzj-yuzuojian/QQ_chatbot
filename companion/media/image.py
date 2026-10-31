"""生图：客户自己的 key，自己的钱。

设计决定：

① key 由客户自己配，不用平台的。
   实测：客户的聊天 key 在 gemini 分组，调 /v1/images/generations
   直接 503「分组 gemini 下模型 gpt-image-2 无可用渠道」。
   聊天和生图通常不在同一个分组，所以必须分开配。
   没配就明确告诉客户"画不了"，绝不偷偷用平台 key。

② 判断"要不要画"交给模型，不用规则。
   规则判断不稳：
       "我昨天拍了张照片" → 会被"照片"误判成要画
       "你画得真好"     → 会被"画"误判
   改成让模型在同一次回复里顺带输出 JSON，零额外调用。

③ 生成后直接传 QQ，不落盘。
   图片本身就几十 KB 到几 MB，存下来只会占磁盘，
   客户要的话让他自己截图保存。
"""
from __future__ import annotations

import base64
import json
import re
import time
from typing import Any

import aiohttp

# 生图模型。gpt-image 系列最稳，成本也低。
DEFAULT_IMAGE_MODEL = "gpt-image-2"

# 尺寸只支持三档（中转站的限制，传别的会 400）
SIZES = {"1024x1024", "1536x1024", "1024x1536"}

# 生图很慢：high 质量要 2-3 分钟。给足时间。
IMAGE_TIMEOUT = 300


async def generate(
    session: aiohttp.ClientSession,
    api_base: str,
    api_key: str,
    prompt: str,
    model: str = DEFAULT_IMAGE_MODEL,
    size: str = "1024x1024",
    n: int = 1,
    timeout: int = IMAGE_TIMEOUT,
) -> tuple[list[bytes], str]:
    """生成图片，返回 (图片二进制列表, 错误信息)。

    错误信息非空表示失败。调用方要据此告诉客户，不能假装画好了。
    """
    if not api_base or not api_key:
        return [], "没配生图 key"

    if size not in SIZES:
        size = "1024x1024"

    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt[:2000],  # 超长 prompt 会 400
        "n": max(1, min(n, 4)),
        "size": size,
    }

    url = f"{api_base.rstrip('/')}/images/generations"
    try:
        async with session.post(
            url,
            json=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as r:
            raw = await r.text()
            if r.status == 401:
                return [], "生图 key 无效"
            if r.status == 402:
                return [], "生图 key 余额不足"
            if r.status == 429:
                return [], "生图太频繁，稍后再试"
            if r.status == 404:
                return [], f"没有 {model} 这个模型"
            if r.status >= 400:
                # 抓"分组...无可用渠道"这类原因，告诉客户是分组问题
                low = raw.lower()
                if "channel" in low or "分组" in raw or "无可用渠道" in raw:
                    return [], "这个 key 的分组没有生图权限"
                return [], f"生图失败（{r.status}）"
    except aiohttp.ClientError:
        return [], "连不上生图服务"
    except Exception:
        return [], "生图超时或出错"

    try:
        data = json.loads(raw)
    except Exception:
        return [], "生图返回看不懂"

    items = data.get("data") or []
    if not items:
        return [], "生图没返回图片"

    out: list[bytes] = []
    for it in items:
        b64 = it.get("b64_json")
        if b64:
            try:
                out.append(base64.b64decode(b64))
            except Exception:
                continue
            continue
        # 有的中转站只给 URL，得再下载一次
        u = it.get("url")
        if u:
            try:
                async with session.get(
                    u, timeout=aiohttp.ClientTimeout(total=60)
                ) as ir:
                    if ir.status == 200:
                        out.append(await ir.read())
            except Exception:
                continue

    if not out:
        return [], "图片下载失败"
    return out, ""


# ----------------------------------------------------------------------
# 意图判断：从模型回复里抠出结构化指令
# ----------------------------------------------------------------------

# 提示模型在需要画图时附带这一行。
# 用独立的一行而不是 JSON 块，模型更不容易写错。
IMAGE_MARKER = "[[DRAW:"


def image_hint() -> str:
    """给 system prompt 追加的说明。

    为什么用标记而不是纯 JSON：
        聊天回复是自由文本，硬塞 JSON 会让人话变得别扭。
        单独一行标记，模型写起来自然，正则一抠就出来。
    """
    return (
        "\n【画图能力】\n"
        "- 客户让你画图/生成图片时，先正常回复，然后**另起一行**写：\n"
        f"  {IMAGE_MARKER}这里写英文或中文的画面描述]]\n"
        "- 例如：\n"
        "  好呀，给你画一只。\n"
        f"  {IMAGE_MARKER}a cute orange cat sitting on a windowsill, warm sunlight, cozy]]\n"
        "- 只在客户明确要图片时才写这一行。聊天、回答问题都不用。\n"
        "- 如果客户没配生图 key，也要照常写这一行——"
        "后端会发现没配，然后告诉客户去配。\n"
    )


# 标记格式：[[DRAW:描述]]
#
# 为什么用贪婪 (.*) 而不是非贪婪 (.*?)：
#     画面描述里可能出现单个方括号，例如 "cat [with hat]"。
#     非贪婪会在第一个 ]] 处停，把内容截成 "cat [with hat"，
#     正文里还会残留一个 "]"，两边都错。
#     贪婪会一直匹配到**最后一个** ]]，单条指令时结果正确。
#     代价：一次回复里写两条 [[DRAW:...]] 会合并成一条——
#     但实际不会发生（一次只画一张），可接受。
_MARKER_RE = re.compile(r"\[\[DRAW:(.*)\]\]", re.S)


def extract_draw(text: str) -> tuple[str, str]:
    """从模型回复里拆出 (纯文本, 画面描述)。

    没有标记时返回 (原文, "")。
    """
    if not text:
        return "", ""
    m = _MARKER_RE.search(text)
    if not m:
        return text.strip(), ""
    prompt = m.group(1).strip()
    clean = _MARKER_RE.sub("", text).strip()
    # 去掉标记后可能留下空行
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, prompt


# ----------------------------------------------------------------------
# 传 QQ
# ----------------------------------------------------------------------


async def upload_to_qq(
    session: aiohttp.ClientSession,
    token: str,
    openid: str,
    data: bytes,
    is_group: bool = False,
    api_base: str = "https://api.sgroup.qq.com",
) -> str:
    """把图片上传成 QQ 富媒体，返回 file_info。

    单聊用 /v2/users/{openid}/files，群聊用 /v2/groups/{gid}/files。

    实测坑：必须是 **JSON + base64**，不能用 multipart。
        试过 multipart 的 file_data / file 两种字段名，都返回
        40093007「富媒体文件下载失败」——它把分数体当成了 URL 去下载。
        JSON body 里传 base64 才认。
    """
    if not data:
        return ""

    target = "groups" if is_group else "users"
    url = f"{api_base}/v2/{target}/{openid}/files"

    body = {
        "file_type": 1,          # 1 = 图片
        "file_data": base64.b64encode(data).decode(),
        "srv_send_msg": False,   # 只上传，不自动发
    }

    try:
        async with session.post(
            url,
            json=body,
            headers={
                "Authorization": f"QQBot {token}",
                "Content-Type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=90),
        ) as r:
            if r.status >= 400:
                text = await r.text()
                return f"__ERR__{r.status}:{text[:100]}"
            d = await r.json()
            return d.get("file_info") or ""
    except Exception as e:
        return f"__ERR__{type(e).__name__}"
