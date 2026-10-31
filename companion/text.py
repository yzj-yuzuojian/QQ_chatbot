"""文本处理：中文 bigram 切词、查询词提取、敏感信息脱敏。

这个模块是整个检索的地基。所有中文分词的坑都在这里处理。

为什么不用 SQLite 内置分词器（实测结论）：
    unicode61  → "面试" 查不到（中文整段被当一个词）
    trigram    → "面试" 查不到（需要 ≥3 字才建索引）
    → 必须自己切 bigram，存进独立列
"""
from __future__ import annotations

import hashlib
import re

# ---------------------------------------------------------------------------
# 正文切词
# ---------------------------------------------------------------------------

# 只保留中文、字母、数字。标点、emoji、空白全部丢掉。
_KEEP = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]")


def normalize(text: str) -> str:
    """归一化：去标点、转小写。"""
    if not text:
        return ""
    return _KEEP.sub("", text).lower()


def to_tokens(text: str, minlen: int = 1) -> list[str]:
    """切成 bigram + unigram。

    同时保留一元和二元，原因：
        - 二元（面试、香菜）精度高，是主力
        - 一元（猫、狗）用于支持单字查询，否则"猫"永远搜不到

    Args:
        text: 原文
        minlen: 最小长度，1 = 一元二元都要，2 = 只要二元（查询时用，精度优先）
    """
    s = normalize(text)
    if not s:
        return []

    out: set[str] = set()
    for i in range(len(s) - 1):
        out.add(s[i : i + 2])
    if minlen <= 1:
        for ch in s:
            out.add(ch)

    return sorted(t for t in out if len(t) >= minlen)


def bigrams_for_index(text: str) -> str:
    """生成入库用的 bigrams 字段（空白分隔）。"""
    return " ".join(to_tokens(text, minlen=1))


# ---------------------------------------------------------------------------
# 查询词提取
# ---------------------------------------------------------------------------

# 功能词：对检索无贡献，反而制造噪声。
# 顺序重要——长词在前，否则"你"会先把"你还"吃掉。
_FUNC_WORDS = [
    # 疑问/套话
    "你还记得", "你还", "记得", "什么", "怎么", "怎样", "如何", "为什么",
    "这个", "那个", "这些", "那些", "时候", "一下", "一个", "是不是",
    "有没有", "能不能", "可不可以", "告诉",
    # 人称（对检索无意义，且每个用户都有）
    "我们", "你们", "他们", "我", "你", "他", "她", "它",
    # 虚词/助词
    "的", "了", "是", "有", "和", "与", "就", "都", "也",
    "吗", "呢", "吧", "啊", "呀", "嘛", "哦", "嗯", "额",
    "得", "地", "过", "着", "把", "被", "给", "让",
    # 注意：以下单字**不能**放进功能词表，它们会把实义词拆坏
    #   "里" → "哪里" 变 "哪"
    #   "在" → "住在" 变 "住"
    #   "对"/"向"/"什"/"么"/"怎"/"样" → 同理
    # 这些交由 bigram 的语境自行处理，误伤比宽容更糟
]

_FUNC_RE = re.compile(
    "|".join(sorted((re.escape(w) for w in _FUNC_WORDS), key=len, reverse=True))
)

# 虚字集合：用于判断一个 bigram 是否"跨界"。
#
# 判据不是"是不是功能词表里的词"，而是"是不是虚字"。
# 因为跨界发生在虚字两侧：
#   讨厌|吃  →  "厌吃" 跨界，因为"吃"是动词没错，但"厌吃"不是词
#   妈|生日   →  "妈生" 跨界，因为"妈"是实词但"妈生"不是词
#
# 纯靠字表做不到完美（中文没有空格），所以还有一步"原文校验"：
# 这些候选词都会被 FTS 拿去匹配，匹配不到自然不召回，
# 排序时覆盖率也算不上，不会污染结果。
# 这一步只是降低噪声，不是决定性的。
_EXTRA_FUNC_CHARS = "什么怎样时候个这那些一下是不没有能不能可以告诉吃"

_FUNC_CHARS: frozenset[str] = frozenset(
    set(w for w in _FUNC_WORDS if len(w) == 1) | set(_EXTRA_FUNC_CHARS)
)

def query_terms(query: str) -> list[str]:
    """从用户问题里提取检索用的关键词。

    做法（顺序很重要）：
        1. 在**原文**上切 bigram —— 保证每个词都是真实连续的两字
        2. 丢掉含功能词的 bigram（"什么""怎么""我的"…）
        3. 如果还剩词，就是结果
        4. 一个都不剩，退化成单字（去掉功能字后）

    为什么不在"删掉功能词后的文本"上切：
        "我讨厌吃什么菜" 删"什么" → "讨厌吃菜" → 切出"厌吃"，
        但用户原文里没有"厌吃"这两个字，FTS 永远搜不到。
        先在原文切、再筛，从根上避免伪 bigram。

    返回空列表表示"提不出有效词，应该走查询改写"。
    """
    norm = normalize(query)
    if not norm:
        return []

    # ① 原文切 bigram
    raw = to_tokens(norm, minlen=2)

    # ② 丢掉"跨界"的 bigram
    #    两种跨界：
    #      a) 整个词命中多字功能词（"什么""怎么"）
    #      b) 任一字是功能单字（"吃什""么菜""妈生"）
    keep = [
        t
        for t in raw
        if not _FUNC_RE.search(t) and not (set(t) & _FUNC_CHARS)
    ]

    # ③ 去掉纯数字（对语义检索无帮助）
    keep = [t for t in keep if not t.isdigit()]

    # ④ 补上"孤立实字"
    #    场景："你还记得我的猫吗" —— "猫"只有一个字，两侧都是虚字，
    #    切不出任何 bigram，但它是这句话唯一有检索价值的词。
    #    规则：虚字夹缝里的单字实词，补进结果。
    for seg in _FUNC_RE.split(norm):
        seg = seg.strip()
        if len(seg) == 1 and not seg.isdigit() and seg not in _FUNC_CHARS:
            keep.append(seg)

    if keep:
        # 去重保序
        seen: set[str] = set()
        out: list[str] = []
        for t in keep:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out

    # ④ 退化：单字，去掉功能单字
    singles = [c for c in norm if not _FUNC_RE.fullmatch(c)]
    seen: set[str] = set()
    out: list[str] = []
    for c in singles:
        if c not in seen and not c.isdigit():
            seen.add(c)
            out.append(c)
    return out

def build_match_expr(terms: list[str]) -> str:
    """把词列表拼成 FTS5 的 MATCH 表达式。

    每个词用双引号包裹，防止 FTS5 把 NEAR/AND/OR/- 之类当语法。
    词之间用 OR：只要命中任一词就召回，排序交给上层重排。
    """
    if not terms:
        return ""
    safe = []
    for t in terms:
        # 双引号内不能再有双引号，直接剔除
        t = t.replace('"', "")
        if t:
            safe.append(f'bigrams:"{t}"')
    return " OR ".join(safe)


# ---------------------------------------------------------------------------
# 敏感信息脱敏
# ---------------------------------------------------------------------------

_SCRUB_RULES = [
    # 顺序很重要：URL 必须最先处理。
    # 如果先替换 key 参数成 "[密钥]"，其中的 "]" 会让 URL 正则提前截断，
    # 结果留下一个孤立的 "]"。
    (re.compile(r"https?://[^\s,;)\]\"']+"), "[链接]"),
    # sk-xxx / 48 位 key
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "[密钥]"),
    (re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{48}(?![A-Za-z0-9])"), "[密钥]"),
    # 显式标注的密钥
    (re.compile(r"(?i)(api[_-]?key|token|密钥|秘钥)\s*[:=：]\s*\S{6,}"), "[密钥]"),
    # IP —— 不能用 \b，中文也算 word char，"服务器1.2.3.4" 会匹配不上
    (re.compile(r"(?<![0-9])(?:\d{1,3}\.){3}\d{1,3}(?![0-9])"), "[IP]"),
    # 手机号 —— 同上
    (re.compile(r"(?<![0-9])1[3-9]\d{9}(?![0-9])"), "[手机号]"),
]


def looks_like_key(text: str) -> bool:
    """是否含疑似密钥。宁可误报，不可漏报。"""
    if not text:
        return False
    for pat, _ in _SCRUB_RULES[:3]:
        if pat.search(text):
            return True
    return False


def scrub(text: str) -> str:
    """脱敏后再落库/打日志。"""
    if not text:
        return ""
    s = text
    for pat, rep in _SCRUB_RULES:
        s = pat.sub(rep, s)
    return s


def fingerprint(key: str) -> str:
    """key 指纹：sha256 前 8 位。用于日志与去重，不可逆。"""
    return hashlib.sha256(key.encode("utf-8", "ignore")).hexdigest()[:8]


def mask_key(key: str) -> str:
    """展示用：sk-****abcd"""
    if not key:
        return ""
    tail = key[-4:] if len(key) >= 4 else key
    return f"sk-****{tail}"


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数。

    中文约 1 token / 1.5 字，英文约 1 token / 4 字符。
    精度够用（误差 ±20%），不值得引入真 tokenizer。
    """
    if not text:
        return 0
    cn = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = len(text) - cn
    return int(cn / 1.5 + other / 4)
