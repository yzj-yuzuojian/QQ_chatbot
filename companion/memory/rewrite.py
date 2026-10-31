"""查询改写。

作用：把"我在哪工作"改写成"杭州 前端 开发"，让检索能命中。

为什么必须有：
    用户提问用的词和历史里的词经常不同：
        "我在哪工作"  vs  历史里的"我在杭州做前端开发"
        "我的猫"      vs  历史里的"橘猫叫团子"
    纯关键词匹配在这种情况下召回为零。

成本：用客户自己的 key 调一次便宜模型，约 30 token 输出，可忽略。
      只在 `needs_rewrite()` 为真时才调用，实测约 30% 的检索请求会用到。
"""
from __future__ import annotations

import re
from typing import Any

from ..llm import client as LLM

REWRITE_PROMPT = """把用户的问题改写成用于检索历史对话的关键词。

规则：
1. 只输出关键词，用空格分隔，不要标点、不要解释
2. 3-6 个词，从宽到窄排列
3. 优先输出具体名词（人名、地点、物品、职业、事件）
4. 补上问题里省略但明显需要的词
5. 不要输出"什么""怎么""我""你"这类虚词

示例：
输入：我在哪工作
输出：工作 公司 职业 上班 前端

输入：我的猫叫什么
输出：猫 宠物 名字 养

输入：我什么时候去面试
输出：面试 时间 日期 公司

输入：你还记得我讨厌什么吗
输出：讨厌 不吃 忌口 过敏"""

# 模型有时会自己加解释，这里做清洗
_NOISE = re.compile(r"[，。！？、；：\"'（）\(\)\[\]【】]")

# 改写结果里不该出现的词
_STOP = frozenset(
    "的 了 是 在 有 和 与 就 都 也 还 吗 呢 吧 啊 呀 嘛 我 你 他 她 它 "
    "什么 怎么 怎样 如何 这个 那个 一下 一个 好的 可以 如下 输出 关键词".split()
)


# 模型爱加的套话前缀，留着会污染检索
_PREFIX = re.compile(
    r"^(?:好的|结果|输出|关键词|如下|答案)[：:，,\s]*"
)


def _clean(raw: str) -> list[str]:
    """清洗模型输出为词列表。"""
    s = raw.strip()

    # 去掉代码块标记，但要保留块内内容
    if "```" in s:
        parts = [p.strip() for p in s.split("```") if p.strip()]
        # 取第一个非语言标记的段
        for p in parts:
            if p and not p.isalpha() or len(p) > 3:
                s = p
                break
        else:
            s = parts[0] if parts else ""

    # 只取第一行（模型偶尔会解释）
    s = s.split("\n")[0]
    s = _PREFIX.sub("", s)
    s = _NOISE.sub(" ", s)

    out: list[str] = []
    for w in s.split():
        w = w.strip()
        if not w or w.isdigit():
            continue
        if len(w) > 12:
            continue
        # 过滤掉虚词和套话
        if w in _STOP:
            continue
        out.append(w)
        if len(out) >= 8:
            break
    return out


async def rewrite(
    session: Any,
    api_base: str,
    api_key: str,
    model: str,
    query: str,
    history_hint: str = "",
) -> list[str]:
    """把问题改写成检索词。失败返回空列表（上层退回原词）。"""
    payload = query
    if history_hint:
        payload = f"{query}\n（历史里可能用这些词：{history_hint}）"

    try:
        resp = await LLM.chat(
            session,
            api_base,
            api_key,
            model,
            [
                {"role": "system", "content": REWRITE_PROMPT},
                {"role": "user", "content": payload},
            ],
            temperature=0.3,
            max_tokens=80,
            timeout=20,
        )
    except Exception:
        return []

    return _clean(resp.content)


def expand_terms(orig: list[str], extra: list[str]) -> list[str]:
    """合并原词和改写词，原词优先。

    原词是用户真实说过的，可信度更高；
    改写词是模型猜的，只用来扩大召回。
    """
    seen: set[str] = set()
    out: list[str] = []

    for t in orig + extra:
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)

    return out
