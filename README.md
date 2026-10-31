# QQ 陪伴机器人

带长期记忆的 QQ 聊天机器人。每个客户有自己的对话上下文和记忆，可以设置多个人设随时切换，用自己配置的 API key 付费。

**没有平台兜底 key。** 客户不配置就完全用不了 —— 这是设计选择，不是缺陷。

> **本项目绑定中转站：[oldnewapi.yuzuojian.top](https://oldnewapi.yuzuojian.top)**
> 客户绑定 API Key 时使用该站。想换成自己的站，见[配置说明](#换成自己的中转站)。

```
客户 A ──┐                    ┌── 人设「雪球」（猫）
客户 B ──┼── 各自独立记忆 ────┼── 人设「老周」（发小）
客户 C ──┘                    └── 事实卡（跨人设共享）
```

---

## 它解决什么问题

市面上的 QQ 机器人大多是这样：所有人共用一个人格，聊完就忘，或者只能记住最近几条。

这个项目要做的是**长期陪伴**：它记得你叫林晚、养了两只猫、对芒果过敏，三个月后还能接着聊。换个角色（比如从「猫」切到「发小」）不会串味，但**始终记得你是谁**。

---

## 核心能力

| 能力 | 说明 |
|---|---|
| **分层记忆** | 事实卡（永不丢失）+ 滚动摘要（远期）+ 向量检索（近期细节） |
| **多个人设** | 每人最多 5 个，随时切换，对话线互相独立 |
| **语义检索** | 「我那只猫咋样了」能命中「团子特别能吃」 |
| **客户自备 key** | 每个客户配置自己的中转站和模型，费用自付 |
| **生图** | 客户单独配生图 key，说「画一只猫」就出图 |
| **数据主权** | 客户可导出全部数据、可彻底删除 |
| **零平台成本** | 不配置 key 就无法使用，不消耗运营方额度 |

---

## 快速开始

### 环境要求

- Python **3.12+**（用到现代类型标注和 `match` 语法）
- 一个 QQ 机器人（在 [q.qq.com](https://q.qq.com/) 注册，个人主体即可）
- 一个 OpenAI 兼容的中转站（用于给客户提供模型）

### 安装

```bash
git clone https://github.com/yzj-yuzuojian/QQ_chatbot.git
cd qq-companion

python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 配置

两种方式，**环境变量优先于配置文件**。

#### 方式一：环境变量（推荐，更安全）

```bash
# 生成加密密钥（必须，否则客户 key 会明文存储）
.venv/bin/python -m companion.cli gen-secret

sudo mkdir -p /etc/qq-companion
sudo cp deploy/env.example /etc/qq-companion/env
sudo chmod 600 /etc/qq-companion/env
sudo editor /etc/qq-companion/env
```

必填：

```bash
QQ_APP_ID=你的AppID
QQ_APP_SECRET=你的AppSecret
COMPANION_SECRET=上一步生成的密钥
COMPANION_BIND_URL=https://你的域名/bind
```

#### 方式二：JSON 配置文件

```bash
sudo mkdir -p /opt/qq-companion
sudo cp config.example.json /opt/qq-companion/config.json
sudo chmod 600 /opt/qq-companion/config.json
sudo editor /opt/qq-companion/config.json
```

```json
{
  "app_id": "你的AppID",
  "app_secret": "你的AppSecret",
  "secret": "生成的密钥",
  "embed_api_key": "你的embedding key（可留空）"
}
```

> ⚠️ **`config.json` 里有密钥，不要提交到 git。**
> `.gitignore` 已挡住它，但请自己确认：`git status --short | grep config.json`

#### 换成自己的中转站

中转站地址默认写死在 `companion/config.py`：

```python
RELAY_API_BASE = "https://oldnewapi.yuzuojian.top/v1"
RELAY_SITE_URL = "https://oldnewapi.yuzuojian.top"
```

改成你的站即可。**或者**不改代码，用环境变量覆盖：

```bash
COMPANION_API_BASE=https://your-relay.example.com/v1
COMPANION_SITE_URL=https://your-relay.example.com
```

#### 向量检索（可选）

不配也能跑，只是退回纯关键词检索：

| 配置 | 召回率 |
|---|---|
| 不配 embedding | 70% |
| 配了 embedding | **100%** |

`bge-m3` 在 [SiliconFlow](https://siliconflow.cn) 免费。填 `COMPANION_EMBED_API_KEY` 即可。

### 运行

```bash
# 前台跑。看到 "[gw] READY 登录成功" 就说明连上了
.venv/bin/python -m companion.main
```

### 装成系统服务

```bash
sudo useradd -r -s /bin/false qqcompanion
sudo mkdir -p /var/lib/qq-companion
sudo chown qqcompanion:qqcompanion /var/lib/qq-companion

sudo cp deploy/qq-companion.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qq-companion

# 看日志
sudo journalctl -u qq-companion -f
```

### 验证

```bash
# 用真实 key 跑全功能自测（57 项）
TEST_REAL_KEY=sk-你的密钥 COMPANION_ROOT=. .venv/bin/python tests/selftest.py
```

---

## 客户怎么用

客户第一次发消息会看到隐私告知，然后三步：

```
/setkey      获取绑定链接，在网页上填自己的 API key
/model       选一个模型
直接聊天
```

key 在网页上填，**不会出现在 QQ 聊天记录里**。

完整命令见 [docs/使用手册.md](docs/使用手册.md)。

---

## 架构

```
   QQ 消息
      │
      ▼
┌─────────────────────┐
│ qq/gateway.py       │  WebSocket、事件去重、发消息
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ commands/router.py  │  命令解析、限流、分发
└──────────┬──────────┘
           ▼
┌─────────────────────┐        ┌──────────────────────┐
│ engine.py           │───────▶│ llm/provider.py      │
│ 组装 prompt / 落库   │        │ 客户 key 解析与校验    │
└──────────┬──────────┘        └──────────────────────┘
           ▼
┌─────────────────────┐
│ memory/             │  事实卡 + 摘要 + 向量检索
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ db/store.py         │  SQLite + FTS5
└─────────────────────┘
```

### 模块职责

| 模块 | 行数 | 职责 |
|---|---|---|
| `db/store.py` | ~900 | 数据层：用户、消息、记忆、人设、任务队列 |
| `commands/router.py` | ~960 | 命令解析、限流、各命令实现 |
| `commands/texts.py` | ~150 | 所有面向客户的文案 |
| `qq/gateway.py` | ~380 | WebSocket、事件去重、发消息 |
| `memory/vector.py` | ~380 | 向量索引与语义检索 |
| `llm/provider.py` | ~360 | key 校验、模型列表、三层拦截 |
| `web/bind.py` | ~350 | 绑定页与数据导出页 |
| `memory/retrieve.py` | ~350 | 关键词 + 向量混合检索 |
| `engine.py` | ~340 | 一次对话的完整流程 |
| `main.py` | ~330 | 启动、任务循环、消息分发 |
| `text.py` | ~250 | 中文分词、脱敏、指纹 |
| `media/image.py` | ~240 | 生图与 QQ 富媒体上传 |

---

## 关键设计决策

完整论述见 [docs/设计文档.md](docs/设计文档.md)。这里只列结论。

### 客户必须自备 key

运营方兜底意味着**无限成本**和**合规风险**（替他人调用模型）。客户自备 key 让成本可控、责任清晰。

代码里有三层拦截，启动时若检测到平台级 key 会直接退出：

```
① 命令层：未配置直接拒绝
② ProviderGate.resolve()：无 key 抛 NoProvider
③ assert_no_default_provider()：启动即失败
```

### 不用向量数据库

实测 20 个客户 × 5000 条 = 10 万条，暴力检索 **11ms**。而一次 LLM 回复要 2.5–3.5 秒。

为了省 10 毫秒装几百 MB 依赖，不划算。用 `numpy` 点积 + int8 量化 + 按用户分片加载，内存只涨 3MB。

### embedding 走平台 key（唯一例外）

需要说明清楚：

- 客户自己的聊天 key 里**通常没有 embedding 模型**（实测某站 30 个模型里 0 个）
- embedding 只把文字转成数字，**不生成任何内容**
- `bge-m3` 在 SiliconFlow 免费，成本接近零

不配也就退化成关键词检索，功能不受影响。

### 关闭推理模型的思考过程

实测 `gemini-3.8-flash-high` 回一句「你好」：428 token 里 419 是思考，耗时 5.9 秒。

加上 `reasoning_effort: none` 后 **2.4 秒**，思考 token 归零，抽取质量不变（9/9 → 9/9）。

---

## 已知缺失

**这份清单是诚实版**，上线前请务必了解：

| 缺失 | 影响 | 原因 |
|---|---|---|
| **没有定时提醒** | 客户说「明天 6 点叫我」时机器人会口头答应但做不到 | QQ 被动回复窗口只有 60 分钟，超时需主动消息；主动消息每月配额很少且官方不公开数字 |
| **不能主动发起对话** | 只有客户先说话才能回 | 同上 |
| **群聊支持未验证** | 代码能收群消息，但**没有在真实群里测过** | 尚未加群测试 |
| **群内不共享上下文** | 群里每个人各自独立，机器人不知道「这是一个群」 | 按人区分对话线 |
| **没有敏感词过滤** | 无 | 误报率高，依赖上游模型对齐 |
| **不支持多机部署** | 单机 SQLite，靠文件锁 | 设计目标就是单机小规模（20 客户级） |

详见 [docs/功能与缺失.md](docs/功能与缺失.md)。

---

## 适合什么规模

实测数据（4 核 / 3.6GB 内存）：

| 指标 | 数值 |
|---|---|
| 常驻内存 | 36 MB |
| 20 人并发检索峰值 | +133 MB |
| 单次回复延迟 | 2.5–3.5 秒 |
| 单次检索 | 平均 265ms（20 人并发） |
| 存储 | 20 客户 × 5000 条 ≈ 148 MB |
| 检索召回率 | **100%**（混合检索，20 个用例） |

**适合 20–100 客户的规模。** 再大需要换 PostgreSQL + 独立向量服务。

---

## 文档

| 文档 | 内容 |
|---|---|
| [使用手册](docs/使用手册.md) | 客户怎么用，每个命令干什么 |
| [部署指南](docs/部署指南.md) | 从零到上线、日常运维、**修改代码后怎么上传** |
| [设计文档](docs/设计文档.md) | 为什么这样设计，有哪些取舍 |
| [功能与缺失](docs/功能与缺失.md) | 完整功能清单和已知限制 |

---

## 许可

MIT，见 [LICENSE](LICENSE)。

## 免责

- 本项目仅提供技术实现。使用需遵守 [QQ 机器人运营规范](https://q.qq.com/wiki/) 和当地法律法规
- 对话内容由客户选择的中转站处理，运营方不承担内容责任
- 请勿用于生成违法违规内容
