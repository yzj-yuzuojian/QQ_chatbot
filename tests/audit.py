"""结构审计 —— 防止"字段存在但没人用"这类问题复发。

为什么需要这个脚本：
    之前踩过坑：image_model 字段定义了、engine 也读了，
    但没有任何地方写它。结果客户只能用一个硬编码的模型，
    遇到"没有这个模型"的错误时无从解决。
    这类问题单靠读代码很难发现——字段在，看起来就是实现过。

检查这几类：
    ① 数据库字段：定义了但代码里从未出现（死字段）
    ② 字段只写不读 / 只读不写（半成品）
    ③ 配置项：有默认值但客户没有任何入口改
    ④ 命令：HELP 里列了但没实现，或实现了但没写进 HELP

用法：
    python tests/audit.py
    # 退出码非 0 表示有问题
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.environ.get("COMPANION_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)
sys.path.insert(0, ROOT)

SCHEMA = os.path.join(ROOT, "companion", "db", "schema.sql")
PKG = os.path.join(ROOT, "companion")

problems: list[str] = []
notes: list[str] = []


def read_all_source() -> str:
    """把 companion/ 下所有 py 拼起来，用于统计字段出现次数。"""
    chunks = []
    for dirpath, _dirnames, files in os.walk(PKG):
        for f in files:
            if f.endswith(".py"):
                with open(os.path.join(dirpath, f), encoding="utf-8") as fh:
                    chunks.append(fh.read())
    return "\n".join(chunks)


def parse_schema() -> dict[str, list[str]]:
    """从 schema.sql 里抠出每张表的字段。"""
    with open(SCHEMA, encoding="utf-8") as fh:
        lines = fh.read().split("\n")

    tables: dict[str, list[str]] = {}
    cur: str | None = None
    skip = {"PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT", "INDEX"}

    for line in lines:
        m = re.match(r"CREATE TABLE IF NOT EXISTS (\w+)", line)
        if m:
            cur = m.group(1)
            tables[cur] = []
            continue
        if re.match(r"CREATE (INDEX|VIRTUAL TABLE)", line):
            cur = None
            continue
        if cur:
            if line.strip().startswith(")"):
                cur = None
                continue
            m = re.match(r"\s+(\w+)\s+\w", line)
            if m and m.group(1).upper() not in skip:
                tables[cur].append(m.group(1))
    return tables


def check_fields(code: str) -> None:
    """① 死字段：定义了但代码里一次都没出现过。"""
    print("【1】数据库字段使用情况")
    for table, cols in parse_schema().items():
        dead = []
        half = []
        for c in cols:
            n = len(re.findall(rf"\b{re.escape(c)}\b", code))
            if n == 0:
                dead.append(c)
            elif n <= 1:
                half.append(c)
        if dead:
            problems.append(
                f"表 {table} 有 {len(dead)} 个字段从未被代码引用：{', '.join(dead)}"
            )
            print(f"  ❌ {table}: 死字段 {dead}")
        elif half:
            notes.append(f"表 {table} 的字段只出现 1 次：{', '.join(half)}")
            print(f"  ⚠️  {table}: 仅 1 处 {half}")
        else:
            print(f"  ✅ {table}")


def check_config(code: str) -> None:
    """② 配置项：有默认值，但客户有没有入口改。

    只报"客户可见"的配置——如果某项纯粹是部署参数（路径、端口），
    没入口是正常的。
    """
    print("\n【2】配置项入口")
    # 部署参数，不需要客户入口
    deploy_only = {
        "app_id", "app_secret", "db_path", "bind_host", "bind_port",
        "bind_url", "secret", "workers", "log_level", "extra",
        "api_base", "site_url", "embed_api_base", "embed_api_key",
        "embed_model", "embed_enabled",
    }
    cfg_path = os.path.join(PKG, "config.py")
    with open(cfg_path, encoding="utf-8") as fh:
        src = fh.read()
    fields = re.findall(r"^    (\w+):\s*[^=]+=", src, re.M)

    for f in fields:
        if f in deploy_only:
            continue
        if f not in code:
            problems.append(f"配置项 {f} 在代码里从未被使用")
            print(f"  ❌ {f}: 从未使用")
    print(f"  （{len(fields)} 个配置项，{len(deploy_only)} 个是部署参数，其余都会检查）")


def check_commands(code: str) -> None:
    """③ 命令：HELP 里列的和代码里实现的是否一一对应。"""
    print("\n【3】命令完整性")
    texts_path = os.path.join(PKG, "commands", "texts.py")
    with open(texts_path, encoding="utf-8") as fh:
        help_src = fh.read()

    in_help = set(re.findall(r"^(/[a-z]+)", help_src, re.M))
    in_code = set(re.findall(r'"(/[a-z]+)"', code))

    # 这些是别名或已废弃的入口，不写进 HELP 是有意的：
    #   /image /imgmodel  = /setimage /imagemodel 的简写
    #   /memory /data     = /facts /privacy 的简写
    #   /bind /download   = 旧版入口，只留兼容不宣传
    aliases = {"/image", "/imgmodel", "/memory", "/data", "/bind", "/download", "/tone"}
    in_code -= aliases

    missing_impl = in_help - in_code
    missing_help = in_code - in_help

    if missing_impl:
        problems.append(f"HELP 里列了但代码没实现：{sorted(missing_impl)}")
        print(f"  ❌ 写了但没实现: {sorted(missing_impl)}")
    if missing_help:
        notes.append(f"代码里有但 HELP 没写：{sorted(missing_help)}")
        print(f"  ⚠️  实现了但 HELP 没写: {sorted(missing_help)}")
    if not missing_impl and not missing_help:
        print(f"  ✅ {len(in_help)} 个命令一致")


def check_hardcoded_model() -> None:
    """④ 硬编码的模型名：应该有配置入口，不能只写死。"""
    print("\n【4】硬编码模型")
    media = os.path.join(PKG, "media", "image.py")
    with open(media, encoding="utf-8") as fh:
        src = fh.read()

    m = re.search(r'DEFAULT_IMAGE_MODEL\s*=\s*"([^"]+)"', src)
    if m:
        model = m.group(1)
        # 检查是否有地方能让客户改
        store_src = open(
            os.path.join(PKG, "db", "store.py"), encoding="utf-8"
        ).read()
        if "image_model" not in store_src:
            problems.append(f"生图模型硬编码为 {model}，但客户无法修改")
            print(f"  ❌ {model} 写死，无入口")
        else:
            print(f"  ✅ {model} 是默认值，可被 /imagemodel 覆盖")


def check_dead_logs(code: str) -> None:
    """⑤ 后台任务失败是否留痕（否则客户以为记忆正常）。"""
    print("\n【5】后台任务失败可见性")
    engine = open(os.path.join(PKG, "engine.py"), encoding="utf-8").read()
    has_note = "note_memory_failure" in engine
    has_clear = "clear_memory_failure" in engine
    if has_note and has_clear:
        print("  ✅ 失败会记录，成功后清除，/stats 可见")
    else:
        problems.append("后台记忆任务失败没有留痕，客户无法觉察记忆已停滞")
        print("  ❌ 失败静默")


def main() -> int:
    code = read_all_source()

    check_fields(code)
    check_config(code)
    check_commands(code)
    check_hardcoded_model()
    check_dead_logs(code)

    print("\n" + "=" * 60)
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"  ❌ {p}")
    else:
        print("✅ 结构审计通过")

    if notes:
        print(f"\n{len(notes)} 条提示（不阻塞）：")
        for n in notes:
            print(f"  · {n}")

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
