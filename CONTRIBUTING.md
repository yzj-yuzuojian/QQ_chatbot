# 贡献指南

欢迎提 issue 和 PR。这个项目是一个人维护的，回复可能不快，但每条都会看。

---

## 提 Issue 之前

### 先自查这几点

大部分「机器人不回复」都能自己解决：

| 现象 | 检查 |
|---|---|
| 完全不回复 | `sudo systemctl status qq-companion` |
| 回复报「未配置」 | 发 `/model`，没绑 key 就先 `/setkey` |
| 回复报「额度」 | 发 `/stats` 看余额 |
| 记不住事 | 日志搜 `[vector]`，看 embedding 是否启用 |
| 生图不出图 | 客户需单独 `/setimage`，且 key 要能生图 |

### 带上这些信息

没有这些，很难定位：

```bash
# ① 版本
git log -1 --oneline

# ② 错误日志（去掉客户隐私）
sudo journalctl -u qq-companion -n 100 --no-pager | grep -A10 Traceback

# ③ 环境
python3 --version
uname -a
free -m
```

**不要粘贴**：真实 API key、客户聊天内容、AppSecret。

---

## 提 PR

### 流程

```bash
# ① fork 后克隆
git clone https://github.com/你的用户名/QQ_chatbot.git
cd QQ_chatbot

# ② 建分支（别直接在 main 上改）
git checkout -b fix/群聊不回复

# ③ 改代码
# ...

# ④ 跑测试
.venv/bin/python -m compileall -q companion/
TEST_REAL_KEY=sk-xxx COMPANION_ROOT=. .venv/bin/python tests/selftest.py

# ⑤ 提交
git add -A
git commit -m "fix: 修复群聊消息不回复"

# ⑥ 推送
git push origin fix/群聊不回复
```

然后在 GitHub 上开 PR。

### 提交信息格式

用 [Conventional Commits](https://www.conventionalcommits.org/)：

| 前缀 | 用途 | 例子 |
|---|---|---|
| `feat:` | 新功能 | `feat: 加 /stats 显示余额` |
| `fix:` | 修 bug | `fix: 修复群聊消息不回复` |
| `docs:` | 只改文档 | `docs: 补充部署说明` |
| `refactor:` | 重构不改行为 | `refactor: 拆分 router.py` |
| `chore:` | 杂项 | `chore: 升级 aiohttp` |

### PR 要求

1. **能跑通** —— 至少 `compileall` 过，最好自测也过
2. **说明改了什么、为什么** —— 不要只贴 diff
3. **别混多个不相关的改动** —— 一个 PR 一件事
4. **新功能要更新文档** —— 至少 README 的功能表

---

## 代码约定

### 注释写「为什么」，不写「是什么」

```python
# ❌ 废话
# 如果 msg_id 为空，把 msg_seq 设为 1
if not msg_id:
    payload["msg_seq"] = 1

# ✅ 有价值：解释了非显然的决策
# 没有 msg_id 就别塞空字符串——QQ 会报"msg_id无效"。
# 退化成主动消息，这是唯一能发的办法。
if not msg_id:
    payload["msg_seq"] = 1
```

**判断标准**：读代码的人能不能自己看出「是什么」？能，就别写。看不出「为什么这样做」，必须写。

这个项目的注释密度偏高（约占代码量 25%），因为很多决策反直觉：
为什么不用向量数据库、为什么 bm25 不能当排序器、为什么必须自己切 bigram。
**这些坑不写下来，下一个人会重踩一遍。**

### 类型标注

公开函数要有返回类型：

```python
def get_facts(self, user_id: int, persona_id: str) -> list[dict[str, Any]]:
```

### 异常处理

不要裸 `except:`。要区分「预期内」和「意外」：

```python
# ✅ 预期内：key 解密失败是正常情况（客户可能删了 key）
try:
    key = self.crypto.decrypt(cipher)
except Exception as e:
    self.log(f"[draw] key 解密失败: {e!r}")
    return

# ✅ 意外：记录下来，不要静默
except Exception as e:
    self.log(f"[chat] {type(e).__name__}: {e!r}")
    return "出了点问题，稍后再试。"
```

**永远不要让客户看到一个空的回复。** 失败也要说话。

---

## 架构速查

改代码前建议先读 [设计文档](设计文档.md)，那里解释了每个设计决策的取舍。

```
companion/
├── main.py          启动、任务循环、消息分发
├── engine.py        一次对话的完整流程
├── config.py        配置加载
├── text.py          中文分词、脱敏
├── db/store.py      数据层（SQLite）
├── commands/
│   ├── router.py    命令解析与实现
│   ├── texts.py     所有面向客户的文案  ← 改措辞只动这里
│   └── settings.py  限流参数、保留字
├── memory/
│   ├── facts.py     事实抽取
│   ├── summary.py   滚动摘要
│   ├── retrieve.py  混合检索
│   ├── vector.py    向量索引
│   └── rewrite.py   查询改写
├── llm/
│   ├── provider.py  key 解析与三层拦截  ← 改动要非常小心
│   └── client.py    OpenAI 兼容调用
├── qq/gateway.py    WebSocket、发消息
├── web/bind.py      绑定页
└── media/image.py   生图
```

### 改动风险分级

| 区域 | 风险 | 注意 |
|---|---|---|
| `texts.py` | 🟢 低 | 改措辞随便改 |
| `settings.py` | 🟢 低 | 改参数 |
| `media/image.py` | 🟡 中 | 涉及客户 key 和费用 |
| `memory/` | 🟡 中 | 影响检索质量，改完要跑召回测试 |
| `qq/gateway.py` | 🟠 高 | **必须保持被动回复**，见下 |
| `llm/provider.py` | 🔴 高 | **三层拦截不能破**，见下 |

### 两条不能破的规则

**① 所有发消息必须带 `msg_id`**

```python
# qq/gateway.py
if msg_id:
    payload["msg_id"] = msg_id      # 被动回复，不吃配额
else:
    self.gw.log("[send] ⚠️ 无 msg_id，走主动消息（吃配额）")
```

主动消息每月配额很少。走主动消息几天就会把配额吃光，之后**客户发消息也收不到回复**。

日志里出现 `无 msg_id` 就是 bug，要修。

**② 绝不能引入平台兜底 key**

```python
# llm/provider.py
def assert_no_default_provider() -> None:
    for var in ("PLATFORM_LLM_KEY", "OPENAI_API_KEY", ...):
        if os.environ.get(var):
            raise SystemExit(...)
```

后台任务（摘要、向量索引）不走消息链路，前两层拦截管不住它们。
如果环境里恰好有个 `OPENAI_API_KEY`，就会静默烧运营方的钱。

**这个断言不能删，也不能改成警告。**

---

## 想加功能

先开 issue 讨论，别直接写。

### 优先级

按真实需求排：

| 功能 | 状态 | 前置条件 |
|---|---|---|
| 定时提醒 | 想做 | **必须先测出主动消息配额** |
| 群聊支持 | 代码有基础 | 需要真实群测试 |
| 敏感词过滤 | 待定 | 需要维护词库 |
| 多实例部署 | 不急 | 客户超 100 才需要 |

详见 [功能与缺失](功能与缺失.md)。

### 明确不做

这些被否决过，别浪费时间：

- 内置人物性格（客户要自由定义）
- 订阅制收费（涉及支付和成本承担）
- 向量数据库（20 用户规模用不上，见设计文档 §4）
- torch / sentence-transformers（装完 2GB+）

---

## 文档

改了行为就更新文档：

| 文档 | 什么时候改 |
|---|---|
| `README.md` | 加/删功能、改依赖、改配置项 |
| `docs/使用手册.md` | 客户能看到的行为变了 |
| `docs/部署指南.md` | 配置项、部署步骤变了 |
| `docs/设计文档.md` | 做了新的架构决策 |
| `docs/功能与缺失.md` | 功能状态变了 |

### 已知限制要诚实

如果某个功能做不到，**写在文档里**，不要含糊。

比如定时提醒：代码确实做不到，但模型会口头答应客户。
这个矛盾记录在 `功能与缺失.md` 里，比藏着掖着好。

---

## 许可

提交 PR 即表示同意以 [MIT 许可证](LICENSE) 发布你的贡献。
