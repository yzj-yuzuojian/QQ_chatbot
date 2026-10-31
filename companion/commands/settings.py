"""运行参数：限流、长度限制、保留字。

和 texts.py 分开的原因：
    texts.py 是"对客户说的话"，会频繁改措辞；
    这里是"系统行为参数"，改动要慎重。
    两者的审阅关注点完全不同。
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# 限流与输入限制
# ---------------------------------------------------------------------------
#
# 设计原则：只限"会花钱或会打外部接口"的操作。
# /help /stats /persona /mood /privacy 这些纯本地的命令，爱点多少次都放行——
# 它们不烧客户的 key、不打中转站、不占模型并发，拦它们只会让人烦。

# 单条消息长度上限。
# 为什么限：客户发 5000 字，token 直接爆掉（中文约 1 字 1.5 token），
# 既烧钱又可能超模型上下文，最后还回不出来。
MAX_INPUT_CHARS = 800

RATE_WINDOW = 60          # 统计窗口（秒）
RATE_MAX_CALLS = 30       # 窗口内最多几次"要花钱/打接口"的操作
RATE_DOUBLE_TAP = 0.30    # 低于这个间隔视为误触，静默丢弃（仅对计费操作）

# 需要计费/打外部接口的命令
COSTLY_COMMANDS = frozenset({"/model", "/setkey"})

# 这些词是 /persona 的子命令，不能拿来做名字
PERSONA_RESERVED = frozenset({
    "new", "add", "新建", "del", "delete", "rm", "删", "删除",
    "edit", "改", "set", "off", "clear", "reset", "默认",
    "example", "examples", "demo", "示例", "rename", "改名",
})
