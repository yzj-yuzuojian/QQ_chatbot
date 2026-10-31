"""事实卡：抽取 + 注入。

这是"记忆感"的主要来源。96 字符 / 约 51 token，
能直接答对"我干什么工作""我养了什么""我不吃什么"。

比检索便宜 10 倍，且 100% 准确（因为是结构化存储，不存在召回失败）。
"""
from __future__ import annotations

import json
from typing import Any

from .. import text as T
from ..db.store import Store
from ..llm import client as LLM

# 抽取用便宜模型，异步跑，不影响回复
EXTRACT_PROMPT = """从下面的对话里抽取关于【用户本人】的稳定信息，用于长期记忆。

只抽这几类（没有就跳过，不要编）：
- 称呼/名字：用户怎么称呼自己、希望被怎么叫（最重要，优先抽）
- 职业/工作：做什么工作、在哪、忙不忙
- 居住/家乡：城市、老家
- 家人/关系：父母、兄弟姐妹、伴侣、朋友（只记客观事实）
- 宠物：养了什么、叫什么
- 喜好/忌口：喜欢什么、讨厌什么、过敏
- 作息/习惯：几点睡、日常习惯
- 近况/事件：最近发生的重要事（换了工作、分手、考试、搬家）
- 雷区：明确说过讨厌被怎么称呼/对待

规则：
1. 只抽用户**明确说过**的，不要推断
2. 每条不超过 30 字
3. key 用 2-4 个字的类别名
4. 最多 8 条，按重要性排序
5. 只输出 JSON，不要解释，不要 markdown

输出格式：
{"称呼": "林晚", "职业": "在成都做插画师", "宠物": "两只猫：豆豆、花卷"}"""


async def extract(
    session: Any,
    api_base: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
) -> dict[str, str]:
    """从对话里抽事实。失败返回空 dict，不阻塞主流程。"""
    if not messages:
        return {}

    dialog = "\n".join(
        f"{'用户' if m['role'] == 'user' else '助手'}：{m['content'][:200]}"
        for m in messages[-40:]
    )

    try:
        resp = await LLM.chat(
            session,
            api_base,
            api_key,
            model,
            [
                {"role": "system", "content": EXTRACT_PROMPT},
                {"role": "user", "content": dialog},
            ],
            temperature=0.2,   # 抽取要稳定，不要创造性
            max_tokens=400,
            timeout=60,
        )
    except Exception:
        return {}

    return _parse_facts(resp.content)


def _parse_facts(raw: str) -> dict[str, str]:
    """解析模型输出。模型经常包 ```json，要容忍。"""
    s = raw.strip()

    # 去掉 markdown 代码块
    if "```" in s:
        parts = s.split("```")
        for p in parts:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                s = p
                break

    # 只取第一个完整 JSON 对象
    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end <= start:
        return {}
    s = s[start : end + 1]

    try:
        data = json.loads(s)
    except Exception:
        return {}

    if not isinstance(data, dict):
        return {}

    out: dict[str, str] = {}
    for k, v in data.items():
        if not isinstance(k, str) or not isinstance(v, (str, int, float)):
            continue
        k = k.strip()[:20]
        v = str(v).strip()[:80]
        if k and v:
            out[k] = v
    return out


def render(facts: list[dict[str, Any]]) -> str:
    """把事实卡渲染成 prompt 片段。

    没事实时返回空串，不占 token。
    """
    if not facts:
        return ""
    lines = [f"- {f['key']}：{f['value']}" for f in facts]
    return "## 关于用户\n" + "\n".join(lines)


def token_cost(facts: list[dict[str, Any]]) -> int:
    return T.estimate_tokens(render(facts))


async def refresh(
    store: Store,
    session: Any,
    user_id: int,
    persona_id: str,
    api_base: str,
    api_key: str,
    model: str,
) -> dict[str, str]:
    """重新抽取并覆盖事实卡。

    persona_id='' 表示跨人格共享——"用户叫什么"和"现在扮演谁"无关。
    """
    messages = store.recent_messages(user_id, persona_id, limit=40)
    # 至少要有 2 条（一问一答）才值得抽。
    # 阈值别设太高——新用户第一条消息里就可能有"我叫什么、在哪工作"，
    # 等 4 条才抽会让前几轮显得很笨。
    if len(messages) < 2:
        return {}

    # 把已有事实一起喂进去：模型知道"这些已经记过了"，
    # 就不会因为最近 40 条里没提过"我叫林晚"而把它丢掉。
    # 原来只喂消息，模型看不到旧事实，重抽一次就把早期信息冲没了。
    known = {f["key"]: f["value"] for f in store.get_facts(user_id, "") if f.get("source") != "manual"}
    if known:
        hint = "、".join(f"{k}={v}" for k, v in known.items())
        messages = list(messages) + [
            {
                "role": "user",
                "content": f"（系统提示：你之前已经记住了这些，请保留仍然成立的：{hint}）",
            }
        ]

    facts = await extract(session, api_base, api_key, model, messages)
    if facts:
        # 旧事实 + 新事实合并，新的优先。
        # 原来直接 replace，模型这次没提到的旧事实就永久丢了。
        merged = dict(known)
        merged.update(facts)
        store.replace_facts(user_id, "", merged)
        return merged
    return {}
