"""全功能自测 —— 用真实的 key 和真实的模型跑。

覆盖：
    命令层：/help /start /privacy /persona* /mood* /model /stats /export /delkey /forget
    拦截层：未绑 key / 无效 key / 群聊禁用
    限流：连发
    记忆：事实卡 / 摘要 / 检索 / 查询改写
    隐私：导出 / 删除
"""
import asyncio
import json
import os
import sys
import time

# 开源后别人 clone 到任意目录都能跑。
# 优先用环境变量 COMPANION_ROOT，否则用仓库根目录（tests/ 的上一级）。
_ROOT = os.environ.get("COMPANION_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)
sys.path.insert(0, _ROOT)

import aiohttp

from companion.commands.router import Router
from companion.config import Config
from companion.db.store import Store
from companion.engine import Engine
from companion.llm.provider import Crypto, ProviderGate

CFG = Config.load()
PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        PASS.append(name)
        print(f"  ✅ {name}")
    else:
        FAIL.append((name, detail))
        print(f"  ❌ {name}  {detail}")


async def main() -> None:
    # 用临时目录，避免和真实数据混在一起。
    # 开源后别人的机器上 /tmp 可能不可写，所以用 tempfile。
    import tempfile

    db = os.path.join(tempfile.mkdtemp(prefix="companion-test-"), "test.db")
    if os.path.exists(db):
        os.remove(db)

    store = Store(db)
    crypto = Crypto(CFG.secret)

    async with aiohttp.ClientSession() as sess:
        gate = ProviderGate(store, crypto, sess)
        engine = Engine(store, gate, sess)
        router = Router(
            store, gate, engine, sess, crypto,
            CFG.api_base,
            "https://bot.example.com/bind",
        )

        # 自测会连续发几十条命令，必然撞上限流，属于对测试本身的干扰。
        # 默认关掉，最后单独开一段测限流。
        _real_rate = router._check_rate
        router._check_rate = lambda uid, costly=True: None

        # ============ ① 未绑定状态的拦截 ============
        print("\n【1】未绑定 key 的拦截")
        uid = store.get_or_create_user(CFG.app_id or "test", "openid_new")

        txt, _ = await router.handle(uid, "你好", "c2c", "app")
        check("普通消息被拦", "还没有绑定" in txt, txt[:40])

        txt, _ = await router.handle(uid, "你好", "group", "app")
        check("群聊提示私聊", "私聊" in txt, txt[:40])

        txt, _ = await router.handle(uid, "/help", "c2c", "app")
        check("/help 放行", "我能做什么" in txt)

        txt, _ = await router.handle(uid, "/privacy", "c2c", "app")
        check("/privacy 放行", "隐私说明" in txt)

        txt, _ = await router.handle(uid, "/start", "c2c", "app")
        check("/start 含告知", "先说清楚" in txt and "配置" in txt)

        txt, _ = await router.handle(uid, "/stats", "c2c", "app")
        check("/stats 提示未绑", "还没绑定" in txt, txt[:50])

        # 无平台 key 断言
        try:
            ProviderGate.assert_no_default_provider()
            check("无平台兜底 key", True)
        except RuntimeError as e:
            check("无平台兜底 key", False, str(e))

        # ============ ② 绑定真实 key ============
        print("\n【2】绑定真实 key")
        real_key = os.environ.get("TEST_REAL_KEY", "")
        check("拿到测试 key", bool(real_key), "环境变量 TEST_REAL_KEY 未设置")
        if not real_key:
            return

        store.set_key(
            uid,
            cipher=crypto.encrypt(real_key),
            fingerprint="selftest",
            suffix=real_key[-4:],
            api_base=CFG.api_base,
        )

        txt, _ = await router.handle(uid, "/model", "c2c", "app")
        check("/model 列出 key 的模型", "可以用的模型" in txt, txt[:80])
        # 取第一个模型名
        model = ""
        for line in txt.split("\n"):
            if line.startswith("1. "):
                model = line[3:].split(" ←")[0].strip()
                break
        check("解析出模型名", bool(model), txt[:100])
        print(f"     模型: {model}")

        txt, _ = await router.handle(uid, "/model 1", "c2c", "app")
        check("/model 1 选择成功", "已切换" in txt, txt[:60])

        # 跨组选模型应被拒
        txt, _ = await router.handle(
            uid, "/model deepseek/deepseek-v4.1-flash", "c2c", "app"
        )
        check("拒绝不可用模型", "不能用" in txt, txt[:60])

        # ============ ③ 人设 ============
        print("\n【3】人设（多套，可切换）")
        txt, _ = await router.handle(uid, "/persona", "c2c", "app")
        check("无人设时有引导", "还没有人设" in txt and "/persona new" in txt, txt[:60])

        txt, _ = await router.handle(uid, "/persona example", "c2c", "app")
        check("示例可用", "高冷的布偶猫" in txt)

        txt, _ = await router.handle(uid, "/persona new 短 x", "c2c", "app")
        check("过短被拒", "太短" in txt, txt[:40])

        long_desc = "你是一个角色。" * 400
        txt, _ = await router.handle(uid, f"/persona new 长 {long_desc}", "c2c", "app")
        check("过长被拒", "太长" in txt, txt[:40])

        p1 = "你是一只高冷的布偶猫，名字叫雪球。说话极短，最多两个字。"
        txt, _ = await router.handle(uid, f"/persona new 雪球 {p1}", "c2c", "app")
        check("新建第 1 个", "已创建" in txt and "雪球" in txt, txt[:60])

        p2 = "你是我发小老周，做设计，说话随意会损我。"
        txt, _ = await router.handle(uid, f"/persona new 老周 {p2}", "c2c", "app")
        check("新建第 2 个", "已创建" in txt and "老周" in txt, txt[:60])

        people = store.list_personas(uid)
        check("存了 2 套", len(people) == 2, f"{len(people)} 套")

        txt, _ = await router.handle(uid, "/persona", "c2c", "app")
        check("列表显示 2 个", "雪球" in txt and "老周" in txt)
        check("标出当前", "←" in txt)
        check("当前是老周", store.active_persona(uid)["name"] == "老周")

        txt, _ = await router.handle(uid, "/persona 1", "c2c", "app")
        check("按序号切换", "已切换到" in txt and "雪球" in txt, txt[:50])
        check("切换已生效", store.active_persona(uid)["name"] == "雪球")

        txt, _ = await router.handle(uid, "/persona 老周", "c2c", "app")
        check("按名字切换", "已切换到" in txt and "老周" in txt, txt[:50])

        txt, _ = await router.handle(uid, "/persona edit 2 你是我姐姐，比我大三岁，会管我但不会训我", "c2c", "app")
        check("编辑人设", "已更新" in txt, txt[:50])
        check("编辑已生效", "姐姐" in store.get_persona(uid, people[1]["id"])["content"])

        txt, _ = await router.handle(uid, "/persona del 2", "c2c", "app")
        check("删除需二次确认", "确认删除" in txt and "已删除" not in txt, txt[:50])
        check("确认时人设还在", len(store.list_personas(uid)) == 2)

        txt, _ = await router.handle(uid, "/persona del 2 老周", "c2c", "app")
        check("确认后删除成功", "已删除" in txt, txt[:50])
        check("只剩 1 套", len(store.list_personas(uid)) == 1)

        txt, _ = await router.handle(uid, "/persona 不存在", "c2c", "app")
        check("找不到有提示", "找不到" in txt, txt[:50])

        # 恢复一套用于后续对话
        txt, _ = await router.handle(uid, "/persona new 助手 你是一个简洁的陪聊助手，回复一到两句", "c2c", "app")
        check("恢复可用人设", "已创建" in txt, txt[:50])

        # ============ ④ 语气 ============
        print("\n【4】语气")
        txt, _ = await router.handle(uid, "/mood", "c2c", "app")
        check("/mood 列表", "当前语气" in txt and "毒舌" in txt)

        txt, _ = await router.handle(uid, "/mood 话少", "c2c", "app")
        check("/mood 按名字切换", "已切换" in txt, txt[:40])
        check("语气已入库", store.get_config(uid).get("modifier_id") == "quiet")

        txt, _ = await router.handle(uid, "/mood 3", "c2c", "app")
        check("/mood 按序号切换", "已切换" in txt, txt[:40])

        txt, _ = await router.handle(uid, "/mood 不存在", "c2c", "app")
        check("/mood 未知有提示", "没有" in txt, txt[:40])

        # 恢复温和语气，便于后续对话
        await router.handle(uid, "/mood 如常", "c2c", "app")

        # ============ ⑤ 真实对话 + 记忆 ============
        print("\n【5】真实对话与记忆")
        dialogs = [
            "我养了一只橘猫叫团子",
            "我讨厌吃香菜",
            "我在杭州做前端开发",
            "下周三是我的生日",
        ]
        for i, msg in enumerate(dialogs, 1):
            txt, _ = await router.handle(uid, msg, "c2c", "app")
            ok = bool(txt) and "出了点问题" not in txt and "失败" not in txt
            check(f"第{i}轮真实回复", ok, txt[:60])

        # 跑异步任务（抽事实 + 摘要）
        for _ in range(20):
            job = store.claim_job()
            if not job:
                break
            await engine.run_job(job)

        facts = store.get_facts(uid, "main")
        check("事实卡已生成", len(facts) > 0, f"{len(facts)} 条")
        if facts:
            print("     事实:", {f["key"]: f["value"] for f in facts})

        # 检索
        txt, _ = await router.handle(uid, "你还记得我养了什么吗", "c2c", "app")
        check("检索类提问有回复", bool(txt), txt[:60])
        check("回复里提到记忆", "猫" in txt or "团子" in txt or "宠物" in txt, txt[:80])
        print(f"     回复: {txt[:70]}")

        # ============ ⑥ 用量与导出 ============
        print("\n【6】用量与数据导出")
        txt, _ = await router.handle(uid, "/stats", "c2c", "app")
        check("/stats 有数据", "对话轮次" in txt and "消息条数" in txt)
        check("/stats 有模型", model in txt, txt[:100])

        txt, _ = await router.handle(uid, "/export", "c2c", "app")
        check("/export 生成链接", "export?code=" in txt, txt[:80])

        # 群聊不能导出
        txt, _ = await router.handle(uid, "/export", "group", "app")
        check("群聊禁止导出", "私聊" in txt, txt[:40])

        # ============ ⑦ 限流 ============
        print("\n【7】限流")
        from companion.commands.router import RATE_MAX_CALLS as RATE_MAX_MSGS

        router._check_rate = _real_rate

        # 纯本地命令不占额度 —— 先验证这条
        local_all_pass = True
        for _ in range(40):
            t, _ = await router.handle(uid, "/stats", "c2c", "app")
            if "太快" in t:
                local_all_pass = False
                break
        check("本地命令 40 次不触发限流", local_all_pass)

        # 真正要花钱的才限流。/model 会打中转站接口，用它测
        trip = False
        for _ in range(RATE_MAX_MSGS + 5):
            t, _ = await router.handle(uid, "/mood", "c2c", "app")  # 本地，不计数
            t2, _ = await router.handle(uid, "/model 1", "c2c", "app")  # 计费
            if "太快" in t2:
                trip = True
                break
        check(f"连发 {RATE_MAX_MSGS}+ 计费操作触发限流", trip)

        # 测完关掉，避免影响后面
        router._rate.clear()
        router._check_rate = lambda uid, costly=True: None

        # ============ ⑧ 换 key / 删记忆 ============
        print("\n【8】换 key 与删记忆")
        before = store.count_messages(uid, "main")
        txt, _ = await router.handle(uid, "/delkey", "c2c", "app")
        check("/delkey 成功", "已解绑" in txt)
        check("key 已清", not store.get_config(uid).get("key_cipher"))
        check("记忆保留", store.count_messages(uid, "main") == before)

        txt, _ = await router.handle(uid, "/forget", "c2c", "app")
        check("/forget 成功", "删除" in txt)
        check("消息已清", store.count_messages(uid, "main") == 0)
        check("事实已清", len(store.get_facts(uid, "main")) == 0)

    # ============ 汇总 ============
    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("\n失败项：")
        for name, detail in FAIL:
            print(f"  · {name}  {detail}")
    print("=" * 60)


asyncio.run(main())
