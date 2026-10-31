"""滚动摘要：把早期对话压缩成一段文字。

与酒馆的 Summarize 扩展同思路：
  - 绑定 anchor_message_id，可回滚
  - 明确告诉模型"保留什么、丢什么"

为什么需要：L1（最近 10 轮）之外的内容如果不摘要，
迟早超出上下文窗口被硬砍，那就是真失忆。
"""
from __future__ import annotations

from typing import Any

from .. import text as T
from ..db.store import Store
from ..llm import client as LLM

# 累计多少条新消息触发一次摘要
SUMMARY_EVERY = 20

SUMMARY_PROMPT = """把下面的对话压缩成一段 150-250 字的摘要，供以后回忆用。

必须保留：
- 人物：用户提到的人（家人、朋友、伴侣）及关系
- 事件：发生了什么，结果如何
- 情绪：用户的情绪状态和转折
- 承诺：约定、计划、答应过的事
- 偏好：明确表达的喜欢/讨厌

可以丢弃：
- 寒暄（"在吗""吃了吗"）
- 重复内容
- 助手自己的客套话

用第三人称写，直接描述，不要"这段对话中"这类套话。
只输出摘要正文，不要标题。"""


async def build(
    session: Any,
    api_base: str,
    api_key: str,
    model: str,
    old_summary: str,
    messages: list[dict[str, Any]],
) -> str:
    """生成摘要。传入旧摘要做增量合并，避免反复重写。"""
    if not messages:
        return old_summary

    dialog = "\n".join(
        f"{'用户' if m['role'] == 'user' else '助手'}：{m['content'][:300]}"
        for m in messages
    )

    parts = []
    if old_summary:
        parts.append(f"【已有摘要】\n{old_summary}\n")
    parts.append(f"【新的对话】\n{dialog}")
    parts.append("\n请输出合并后的完整摘要。")
    user_content = "\n".join(parts)

    try:
        resp = await LLM.chat(
            session,
            api_base,
            api_key,
            model,
            [
                {"role": "system", "content": SUMMARY_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0.3,
            max_tokens=500,
            timeout=90,
        )
        return resp.content.strip()[:1200]
    except Exception:
        return old_summary


async def maybe_update(
    store: Store,
    session: Any,
    user_id: int,
    persona_id: str,
    api_base: str,
    api_key: str,
    model: str,
) -> str | None:
    """到达阈值就更新摘要。返回新摘要，没更新返回 None。"""
    last = store.latest_summary(user_id, persona_id)
    covered = int(last["covered_until"]) if last else 0

    total = store.count_messages(user_id, persona_id)
    if total - covered < SUMMARY_EVERY:
        return None

    # 取"上次覆盖之后、最近 10 条之前"的消息
    cur = store.conn.execute(
        """SELECT id, role, content, created_at FROM messages
           WHERE user_id=? AND persona_id=? AND id > ?
           ORDER BY id""",
        (user_id, persona_id, covered),
    )
    rows = [dict(r) for r in cur.fetchall()]
    if len(rows) <= 10:
        return None   # 剩下的不够压，留给下次

    to_compress = rows[:-10]   # 最近 10 条保留原文
    if not to_compress:
        return None

    old = last["content"] if last else ""
    new_summary = await build(session, api_base, api_key, model, old, to_compress)
    if not new_summary:
        return None

    anchor = int(to_compress[-1]["id"])
    store.add_summary(
        user_id, persona_id, new_summary, anchor, covered_until=anchor
    )
    return new_summary


def render(summary: dict[str, Any] | None) -> str:
    if not summary:
        return ""
    body = (summary.get("content") or "").strip()
    if not body:
        return ""
    return "## 之前聊过\n" + body


def token_cost(summary: dict[str, Any] | None) -> int:
    return T.estimate_tokens(render(summary))
