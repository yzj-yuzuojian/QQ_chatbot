"""向量记忆：语义检索。

为什么需要（实测数据）：
    用 20 个口语化问题测关键词检索，召回 70%。
    6 个失败的**全部**是"换了个说法"：
        问「我那只猫咋样了」 → 记忆里是「团子特别能吃」  （零共同词）
        问「我锻炼身体的事」 → 记忆里是「最近在健身」    （同义词）
        问「我住的地方」   → 记忆里是「租的房子在城西」  （不同表述）
    换成向量后 95%。这不是微调，是能力差异。

三个关键设计决定：

① 用云端 API 算向量，不本地跑模型。
   这台机器 3.6G 内存，装 torch 要 1.5-2G，会 OOM。
   云端算向量 = 服务器零内存开销。

② int8 量化存储。
   float32 每条 4KB → int8 每条 1KB，磁盘和内存都省 4 倍。
   精度损失实测 <1%（召回不变）。

③ 按用户分片加载，不做全量常驻。
   20 个客户每人 5000 条，全量 100MB 常驻会吃满小机器。
   每次只加载当前用户的（5MB），用完即释放。

不做向量数据库：
    10 万条暴力检索实测 11ms（numpy 点积）。
    这个规模上 FAISS / Chroma 是过度设计，还得多装几百 MB 依赖。
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import aiohttp

import numpy as np

# 向量模型。bge-m3：中文第一梯队、免费、1024 维。
# 换模型只需改这里 + 清空 message_vectors 重算。
EMBED_MODEL = "BAAI/bge-m3"
EMBED_DIM = 1024

# int8 量化后每条向量 1024 字节
BATCH_SIZE = 32          # 一次请求最多几条（硅基流动支持批量，省往返）
# 单用户最多加载多少条向量。
#
# 为什么不设更大：记忆是分层的——
#   事实卡   → 姓名、宠物、职业这些长期事实（永不丢失）
#   摘要     → 每 20 轮压缩一次，覆盖远期
#   向量     → 负责**近期细节**（说了什么原话）
# 所以向量不需要覆盖全部历史。2000 条按每天 125 条算约 16 天，
# 足够覆盖"上周说的那句话"这类问题。
#
# 代价对比（20 人并发）：
#   6000 条 → 每人 48ms 计算
#   2000 条 → 每人约 17ms 计算，快 2.8 倍
MAX_LOAD = 2500


# ----------------------------------------------------------------------
# int8 量化
# ----------------------------------------------------------------------


def quantize(vec: np.ndarray) -> bytes:
    """float32 → int8。

    bge-m3 的输出已经是 L2 归一化过的（模长约 1），
    每个分量落在 [-1, 1]。乘 127 取整即可，误差 < 1/127。

    为什么不做 per-vector min/max 归一化：
        那样每条要额外存 min/max 两个 float，反而多 8 字节，
        而且归一化后的向量不能直接点积算余弦相似度。
    """
    q = np.clip(np.round(vec * 127.0), -127, 127).astype(np.int8)
    return q.tobytes()


def dequantize(blob: bytes, dim: int) -> np.ndarray:
    """int8 → float32，并重新归一化。

    量化会引入微小模长偏差，归一化后才能用点积当余弦相似度。
    """
    q = np.frombuffer(blob, dtype=np.int8).astype(np.float32) / 127.0
    n = np.linalg.norm(q)
    return q / n if n > 0 else q


# ----------------------------------------------------------------------
# 调 API 算向量
# ----------------------------------------------------------------------


async def embed(
    session: Any,
    api_base: str,
    api_key: str,
    texts: list[str],
    model: str = EMBED_MODEL,
) -> list[np.ndarray]:
    """批量算向量。失败返回空列表，不抛异常。

    为什么静默失败：向量是"锦上添花"的能力，
    挂了也应该退化成关键词检索，而不是让客户聊不了天。
    """
    if not texts:
        return []

    body = {"model": model, "input": texts}
    try:
        async with session.post(
            f"{api_base.rstrip('/')}/embeddings",
            json=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=60),
        ) as r:
            if r.status >= 400:
                return []
            data = await r.json()
    except Exception:
        return []

    items = data.get("data") or []
    if not items:
        return []

    # 按 index 排序，防止接口乱序返回
    items.sort(key=lambda x: x.get("index", 0))
    out = []
    for it in items:
        v = it.get("embedding")
        if not v:
            continue
        arr = np.array(v, dtype=np.float32)
        n = np.linalg.norm(arr)
        out.append(arr / n if n > 0 else arr)
    return out


async def embed_chunked(
    session: Any,
    api_base: str,
    api_key: str,
    texts: list[str],
    model: str = EMBED_MODEL,
) -> list[np.ndarray]:
    """分批算，避免单次请求太大被拒。"""
    out: list[np.ndarray] = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        got = await embed(session, api_base, api_key, batch, model)
        if not got:
            break
        out.extend(got)
    return out


# ----------------------------------------------------------------------
# 存取
# ----------------------------------------------------------------------


async def index_messages(
    store: Any,
    session: Any,
    api_base: str,
    api_key: str,
    user_id: int,
    limit: int = 200,
    model: str = EMBED_MODEL,
) -> int:
    """给还没向量的消息补上向量。返回生成了几条。

    调用时机：每条消息写库后异步触发（不阻塞回复），
    以及启动时补历史。
    """
    rows = store.messages_without_vector(user_id, limit)
    if not rows:
        return 0

    texts = [r["content"][:2000] for r in rows]  # 超长截断，省 token
    vecs = await embed_chunked(session, api_base, api_key, texts, model)
    if not vecs:
        return 0

    n = 0
    for row, vec in zip(rows, vecs):
        store.save_vector(row["id"], user_id, quantize(vec), len(vec), model)
        n += 1
    return n


async def index_message_one(
    store: Any,
    session: Any,
    api_base: str,
    api_key: str,
    user_id: int,
    message_id: int,
    content: str,
    model: str = EMBED_MODEL,
) -> bool:
    """单条消息立即生成向量（聊天主路径用，异步调用）。"""
    vecs = await embed(session, api_base, api_key, [content[:2000]], model)
    if not vecs:
        return False
    store.save_vector(message_id, user_id, quantize(vecs[0]), len(vecs[0]), model)
    return True


def build_matrix(rows: list[dict[str, Any]]) -> tuple[np.ndarray, list[dict]]:
    """把 BLOB 拼成一个矩阵，用于批量点积。

    返回 (矩阵, 对应的行元数据)。
    维度不一致的行会被跳过（换模型后可能出现）。

    注意：这个函数会把所有向量反量化成 float32，10 万条要 391MB。
    大规模场景请用 score_chunked()，它分块处理。
    """
    vecs: list[np.ndarray] = []
    meta: list[dict] = []
    for r in rows:
        if r["dim"] != EMBED_DIM:
            continue
        try:
            v = dequantize(r["vec"], r["dim"])
        except Exception:
            continue
        vecs.append(v)
        meta.append(r)
    if not vecs:
        return np.zeros((0, EMBED_DIM), dtype=np.float32), []
    return np.vstack(vecs), meta


# numpy 计算的线程池。
#
# 为什么必须要线程池：
#     asyncio 是单线程事件循环，numpy 的点积计算**不会**让出 GIL。
#     20 个客户同时检索时，实测串行执行 963ms——
#     这期间整个事件循环卡住，其他人的消息连读都读不了。
#     丢进线程池后 numpy 内部会释放 GIL，真正的多核并行，
#     而且事件循环不会被堵。
#
# 4 线程 = 这台机器的核数。再多没有收益（CPU 打满）。
_CPU_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="vec")


# 每块多少条。
#
# 内存峰值 = SCORE_CHUNK × 1024 × 4 字节（float32 转换后）
#          + SCORE_CHUNK × 1024 字节（int8 join 临时）
# 取 4000 时：16MB + 4MB = 20MB 峰值，非常安全。
#
# 为什么不取更大：这台机器总共 3.6G，DSH 常驻 1.3G，
# 机器人自己还有 aiohttp 缓冲。留足余量比省几毫秒重要。
# 4000 条一块时，即使 10 万条也只需 25 次循环，开销可忽略。
SCORE_CHUNK = 4_000


def score_chunked(rows: list[dict[str, Any]], q: np.ndarray) -> tuple[np.ndarray, list[dict]]:
    """分块 + 批量反量化算相似度。

    两个优化叠在一起，缺一不可：

    ① 批量反量化（本函数的核心）
       原来对每条向量单独调 dequantize()，1000 条就是 1000 次
       numpy 小操作，每次都有解释器开销。
       改成把整块的 blob 用 b"".join() 拼成一个大 buffer，
       再一次性 frombuffer + astype，几千条变成一次操作。
       实测 1000 条：14.7ms → 2.0ms。

    ② 分块（控制内存）
       一次性处理 10 万条要 391MB。这台机器只有 2G 可用，
       加上 DSH 自己 1.3G，很容易把进程顶到 OOM。
       分块后峰值固定，与总条数无关。

    注意：b"".join() 会临时复制一份 int8 数据（1000 条约 1MB，
    2 万条约 20MB），这个代价远小于逐条处理的开销。

    返回 (相似度数组, 有效行元数据)。
    """
    n = len(rows)
    # 无效行给最低分，保证不会被选中
    scores = np.full(n, -2.0, dtype=np.float32)
    meta: list[dict] = []

    for start in range(0, n, SCORE_CHUNK):
        block = rows[start : start + SCORE_CHUNK]

        blobs: list[bytes] = []
        idx_map: list[int] = []
        for i, r in enumerate(block):
            if r["dim"] != EMBED_DIM:
                continue
            blob = r["vec"]
            # 长度必须是 dim，否则 frombuffer 会切错位置
            if not blob or len(blob) != EMBED_DIM:
                continue
            blobs.append(blob)
            idx_map.append(start + i)
            meta.append(r)

        if not blobs:
            continue

        # 一次性拼成连续内存，再整体转 float32 并归一化。
        # 拼出来的长度 = 条数 × 1024，reshape 一定对齐。
        raw = b"".join(blobs)
        mat = (
            np.frombuffer(raw, dtype=np.int8)
            .reshape(len(blobs), EMBED_DIM)
            .astype(np.float32)
        )
        mat /= 127.0

        # 逐行归一化（量化会带来微小模长偏差）
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        mat /= norms

        scores[idx_map] = mat @ q

    return scores, meta


async def semantic_search(
    store: Any,
    session: Any,
    api_base: str,
    api_key: str,
    user_id: int,
    query: str,
    topk: int = 5,
    model: str = EMBED_MODEL,
) -> list[dict[str, Any]]:
    """语义检索。返回 [{content, role, score, created_at}]。

    失败或无数据时返回空列表，调用方应退化成关键词检索。
    """
    qvecs = await embed(session, api_base, api_key, [query], model)
    if not qvecs:
        return []
    q = qvecs[0]

    rows = store.get_vectors(user_id, MAX_LOAD)
    if not rows:
        return []

    # 只保留维度匹配的行（换模型后可能出现旧维度）
    valid = [r for r in rows if r["dim"] == EMBED_DIM]
    if not valid:
        return []

    # 分块算，峰值内存固定 20MB，不随消息数增长。
    # 必须丢线程池：numpy 计算不让出 GIL，直接跑会堵住整个事件循环，
    # 20 人并发时所有人一起卡到秒级。
    loop = asyncio.get_running_loop()
    sims, meta = await loop.run_in_executor(_CPU_POOL, score_chunked, valid, q)
    if not meta:
        return []

    k = min(topk, len(sims))
    pick = np.argpartition(-sims, k - 1)[:k]
    pick = pick[np.argsort(-sims[pick])]

    return [
        {
            "content": valid[int(i)]["content"],
            "role": valid[int(i)]["role"],
            "score": float(sims[int(i)]),
            "created_at": valid[int(i)]["created_at"],
        }
        for i in pick
    ]
