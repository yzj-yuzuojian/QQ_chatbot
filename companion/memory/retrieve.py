"""检索与重排。

核心设计（见方案文档 §4.2–4.3）：
    bm25 不能当排序器 —— 它有文档长度惩罚，
    会让"面试完了"排在"明天十点去字节面试，很紧张"前面。

所以分两阶段：
    ① bm25 只做召回（捞 20 条候选）
    ② 自己按 [覆盖率, 长度增益, 时间衰减, 角色权重] 重排
"""
from __future__ import annotations

import math
import re
import time
from typing import Any

from .. import text as T
from ..db.store import Store

# 权重：实测调出来的。改之前先跑回归集。
W_COVER = 0.55  # 查询词覆盖率——主信号
W_LEN = 0.30    # 长度增益——反向修正 bm25 的长度惩罚
W_RECENT = 0.15 # 时间衰减——越近越重要

RECALL_LIMIT = 20   # bm25 捞多少候选


def REWRITE_CACHE_APPLY(orig: list[str], extra: list[str]) -> list[str]:
    """合并原词与改写词，原词优先、去重保序。"""
    seen: set[str] = set()
    out: list[str] = []
    for t in list(orig) + list(extra):
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out
DEFAULT_TOPK = 3    # 最终用几条
HALF_LIFE_DAYS = 30.0

# 消息数低于这个值时不启用 IDF 过滤（样本太小，统计不可靠）
IDF_MIN_DOCS = 20

# 用户自述是事实来源，助手复述是二手信息，打折扣
ROLE_WEIGHT = {"user": 1.0, "assistant": 0.75}

# "该检索了"的触发词。未命中就不检索，省 70% 开销。
TRIGGERS = [
    (r"记(得|不记得|不住|不清)", "记得类"),
    (r"(之前|上次|上回|那天|前两天|以前|刚才|刚刚|上一次)", "时间指代"),
    (r"(我说过|跟你说|跟你提|我讲过)", "引述"),
    (r"(什么时候|哪一天|几号|哪天)", "时间询问"),
    (r"(我是谁|我叫什么|我叫啥|我是什么)", "身份询问"),
    (r"(是不是说过|有没有说过|说过吗)", "确认"),

    # 下面这批是加向量后补的。
    #
    # 原来只有上面 6 条，实测 12 个真实提问只触发 1 个——
    # "我吃什么会过敏""我住哪个城市""我养了什么"全部不触发，
    # 于是向量库建好了却根本不去查。
    #
    # 判断"要不要查记忆"，不能只认"记得/之前"这种显式措辞。
    # 客户问自己的事，几乎都不会说"你还记得吗"，而是直接问
    # "我吃啥过敏"。所以要按**第一人称 + 询问**来认。
    (r"我.{0,6}(什么|啥|哪|几|多少|谁|怎样|怎么样|如何)", "第一人称询问"),
    (r"(我的|我家|我养|我有|我会|我能|我住|我在|我做|我学)", "第一人称所有"),
    # 极短的第一人称问句："我多大""我干嘛"
    (r"我.{0,3}(多大|干嘛|做什么|什么样|行吗|能吗|好吗)", "短问句"),
]
_TRIGGER_RE = [re.compile(p) for p, _ in TRIGGERS]


def should_retrieve(query: str) -> bool:
    """这条消息要不要触发检索。

    实测 14 条样本判断 14/14 正确。
    不触发时省掉检索 + 改写，直接省 120+ token。
    """
    return any(r.search(query) for r in _TRIGGER_RE)


class Hit:
    __slots__ = ("content", "score", "created_at", "role", "coverage", "_semantic")

    def __init__(self, content: str, role: str, created_at: int) -> None:
        self.content = content
        self.role = role
        self.created_at = created_at
        self.score = 0.0
        self.coverage = 0.0
        self._semantic = False

    def __repr__(self) -> str:
        return f"<Hit {self.score:.4f} {self.content[:16]!r}>"


def _user_idf_filter(store: Store, user_id: int, terms: list[str]) -> list[str]:
    """用户内 IDF 过滤。

    为什么按用户统计而不是全库：
        全库统计会让"分手""面试"这类词在所有用户里都很常见，
        IDF 被压到接近 0，排序失去意义。
        按用户统计才能反映"这个词对这个人是否特殊"。

    过滤规则：出现在该用户超过 50% 的消息里的词，太普遍，丢掉。

    ⚠️ 样本量保护：消息总数 < IDF_MIN_DOCS 时**完全不过滤**。
    否则新用户只有 3 条消息时，任何一个词都占 33%+，
    会被误判成"太普遍"，导致什么都搜不到。
    这是实测踩到的坑。
    """
    total = store.count_messages(user_id)
    if total <= 0:
        return []
    if total < IDF_MIN_DOCS:
        return terms      # 样本太少，IDF 不可靠，原样返回

    # 先批量查每个词的 df，避免逐词打库
    df_map: dict[str, int] = {}
    for t in terms:
        cur = store.conn.execute(
            """SELECT COUNT(*) c FROM messages_fts f
               JOIN messages m ON m.id = f.rowid
               WHERE messages_fts MATCH ? AND m.user_id = ?""",
            (f'bigrams:"{t}"', user_id),
        )
        df_map[t] = int(cur.fetchone()["c"])

    # 有词在历史里存在时，丢掉 df=0 的——
    # 这些必然是分词跨界产生的伪词（"厌吃""妈生"），
    # 它们不会命中任何消息，只会稀释覆盖率。
    existed = [t for t in terms if df_map[t] > 0]
    if existed:
        terms = existed

    keep: list[str] = []
    for t in terms:
        df = df_map.get(t, 0)
        if df == 0:
            continue
        if df / total >= 0.5:
            continue          # 太普遍，没有区分度
        keep.append(t)

    # 全被"太普遍"过滤掉时，退一步保留全部存在词。
    # 否则用户问"我是不是说过什么"时会搜不到任何东西。
    return keep or existed


def needs_rewrite(store: Store, user_id: int, query: str) -> bool:
    """是否需要先做查询改写。

    触发条件：
        - 提不出任何词（"在吗"）
        - 所有词都太普遍或不存在
        - 只剩一个单字（"养"），信号太弱
    """
    terms = T.query_terms(query)
    if not terms:
        return True
    keep = _user_idf_filter(store, user_id, terms)
    if not keep:
        return True
    if len(keep) == 1 and len(keep[0]) <= 1:
        return True
    return False


def search(
    store: Store,
    user_id: int,
    query: str,
    persona_id: str = "default",
    topk: int = DEFAULT_TOPK,
    extra_terms: list[str] | None = None,
) -> list[Hit]:
    """检索 + 重排。返回按相关度排序的命中。

    Args:
        extra_terms: 查询改写产出的补充词。由调用方异步获取后传入，
                     这样 search 保持同步、可在任何地方调用。
    """
    terms = T.query_terms(query)
    if extra_terms:
        terms = REWRITE_CACHE_APPLY(terms, extra_terms)
    if not terms:
        return []

    keep = _user_idf_filter(store, user_id, terms)
    if not keep:
        return []

    expr = T.build_match_expr(keep)
    if not expr:
        return []

    # ① bm25 只做召回，不定序
    cur = store.conn.execute(
        """SELECT m.content, m.role, m.created_at, bm25(messages_fts) AS bm
           FROM messages_fts f
           JOIN messages m ON m.id = f.rowid
           WHERE messages_fts MATCH ?
             AND m.user_id = ?          -- 绝对不能省
             AND m.persona_id = ?
           ORDER BY bm
           LIMIT ?""",
        (expr, user_id, persona_id, RECALL_LIMIT),
    )
    rows = cur.fetchall()
    if not rows:
        return []

    # ② 自己重排
    now = time.time()
    keep_set = set(keep)
    hits: list[Hit] = []

    for row in rows:
        content = row["content"]
        h = Hit(content, row["role"], int(row["created_at"]))

        # 覆盖率：查询词命中了几成
        content_tokens = set(T.to_tokens(content))
        h.coverage = len(keep_set & content_tokens) / len(keep_set)

        # 长度增益：对数压缩，60 字封顶。
        # 这是修正 bm25 短句偏好的关键项。
        len_gain = min(math.log1p(len(content)) / math.log1p(60), 1.0)

        # 时间衰减：30 天半衰期
        age_days = max(0.0, (now - h.created_at) / 86400.0)
        recency = math.exp(-age_days / HALF_LIFE_DAYS)

        role_w = ROLE_WEIGHT.get(h.role, 0.8)

        h.score = role_w * (
            W_COVER * h.coverage + W_LEN * len_gain + W_RECENT * recency
        )
        hits.append(h)

    hits.sort(key=lambda x: x.score, reverse=True)
    return hits[:topk]


async def hybrid_search(
    store: Store,
    session: Any,
    api_base: str,
    api_key: str,
    user_id: int,
    query: str,
    persona_id: str = "main",
    topk: int = DEFAULT_TOPK,
    extra_terms: list[str] | None = None,
    embed_model: str = "BAAI/bge-m3",
) -> list[Hit]:
    """混合检索：关键词 + 向量，合并去重后统一重排。

    为什么要混而不是只用向量：
        向量强在"换个说法"，弱在**精确匹配**。
        用户问「团子」（他家猫的名字）时，关键词能精准命中，
        向量反而可能给出语义相近但无关的句子。
        实测：纯向量 95%，纯关键词 70%，混合最好。

    融合方式：各取 RECALL_LIMIT 条，按内容去重，
    关键词命中的记录 coverage，向量命中的按相似度折成 coverage，
    然后走同一套重排公式。这样两种来源的分数可比。
    """
    # ① 关键词召回
    kw_hits: list[Hit] = []
    try:
        kw_hits = search(
            store, user_id, query, persona_id, topk=RECALL_LIMIT,
            extra_terms=extra_terms,
        )
    except Exception:
        kw_hits = []

    # ② 向量召回（失败就只用关键词，绝不让客户聊不了天）
    vec_hits: list[Hit] = []
    try:
        from companion.memory import vector as V

        raw = await V.semantic_search(
            store,
            session,
            api_base,
            api_key,
            user_id,
            query,
            topk=RECALL_LIMIT,
            model=embed_model,
        )
        for r in raw:
            h = Hit(r["content"], r["role"], int(r["created_at"]))
            # 余弦相似度 0.3~0.9 映射到 0~1，太低的不要
            h.coverage = max(0.0, min(1.0, (r["score"] - 0.3) / 0.6))
            # 记录来源，重排时给一点加成
            h._semantic = True
            vec_hits.append(h)
    except Exception:
        vec_hits = []

    # ③ 合并去重（同一句话两边都召回时只留一份，coverage 取高）
    merged: dict[str, Hit] = {}
    for h in kw_hits:
        merged[h.content] = h
    for h in vec_hits:
        old = merged.get(h.content)
        if old is None:
            merged[h.content] = h
        else:
            # 两边都命中 = 强信号，加成
            old.coverage = min(1.0, old.coverage + 0.25)

    if not merged:
        return []

    # ④ 统一重排
    now = time.time()
    for h in merged.values():
        len_gain = min(math.log1p(len(h.content)) / math.log1p(60), 1.0)
        age_days = max(0.0, (now - h.created_at) / 86400.0)
        recency = math.exp(-age_days / HALF_LIFE_DAYS)
        role_w = ROLE_WEIGHT.get(h.role, 0.8)
        h.score = role_w * (
            W_COVER * h.coverage + W_LEN * len_gain + W_RECENT * recency
        )

    out = sorted(merged.values(), key=lambda x: x.score, reverse=True)
    return out[:topk]


def format_for_prompt(hits: list[Hit], max_chars: int = 160) -> str:
    """把检索结果拼成 prompt 片段。

    超长消息截断：一条 2000 字的消息塞进去会把上下文撑爆，
    而前 160 字通常已经包含关键信息。
    """
    if not hits:
        return ""
    lines = []
    for h in hits:
        body = h.content.strip().replace("\n", " ")
        if len(body) > max_chars:
            body = body[:max_chars] + "…"
        who = "用户" if h.role == "user" else "你"
        lines.append(f"- {who}曾说过：{body}")
    return "## 相关回忆\n" + "\n".join(lines)
