"""配置加载。

优先级：环境变量 > 配置文件 > 代码默认值。

为什么环境变量优先：
    systemd 的 `EnvironmentFile` 权限可以设成 600、属主 root，
    比一个放在项目目录里的 JSON 更难误提交到 git。
    生产部署建议用环境变量注入密钥，配置文件只放非敏感参数。
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 中转站地址
# ---------------------------------------------------------------------------
# 本项目默认绑定到下面这个中转站。客户绑定 key 时只能绑这个站。
#
# 想换成自己的站：改这一行即可（改完记得同步 SITE_URL）。
# 之所以写死在代码里而不是做成配置项，是因为这个项目就是为该站做的——
# 允许通过配置改掉会让"默认走这个站"的承诺变得不可靠。
RELAY_API_BASE = "https://oldnewapi.yuzuojian.top/v1"
RELAY_SITE_URL = "https://oldnewapi.yuzuojian.top"


@dataclass
class Config:
    # QQ 机器人凭证（在 q.qq.com 后台拿）
    app_id: str = ""
    app_secret: str = ""

    # 中转站。默认值来自上面的常量，配置文件可覆盖（给 fork 的人留条路）。
    api_base: str = RELAY_API_BASE
    site_url: str = RELAY_SITE_URL

    # 存储
    db_path: str = "/var/lib/qq-companion/data.db"
    bind_host: str = "127.0.0.1"
    bind_port: int = 18401
    bind_url: str = "http://127.0.0.1:18401/bind"

    # 向量记忆（可选）
    #
    # 为什么需要单独一套 key：
    #   客户自己的聊天 key 里通常没有 embedding 模型
    #   （实测某中转站 30 个模型里 0 个 embedding）。
    #   这里用平台 key 只为把文字转成数字，不生成任何内容。
    #
    # 不配也能跑，只是退回纯关键词检索（召回率约 70%，配了是 100%）。
    embed_enabled: bool = True
    embed_api_base: str = "https://api.siliconflow.cn/v1"
    embed_api_key: str = ""
    embed_model: str = "BAAI/bge-m3"

    # 加解密（Fernet 密钥，用于加密客户的 API key）
    secret: str = ""

    # 行为
    workers: int = 2
    log_level: str = "INFO"

    # 配置文件原文，便于将来扩展
    extra: dict[str, Any] = field(default_factory=dict)

    def resolved_site_url(self) -> str:
        """给客户看的站点地址。"""
        if self.site_url:
            return self.site_url.rstrip("/")
        base = (self.api_base or "").rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        return base or "你的中转站"

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        """读取配置。

        配置文件默认在 /opt/qq-companion/config.json，
        可用 COMPANION_CONFIG 指定别的位置。
        文件不存在时全部走默认值 + 环境变量，不报错。
        """
        path = path or os.environ.get(
            "COMPANION_CONFIG", "/opt/qq-companion/config.json"
        )
        raw: dict[str, Any] = {}

        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception as e:
                sys.exit(f"配置文件解析失败 {path}: {e}")
            if not isinstance(raw, dict):
                sys.exit(f"配置文件 {path} 必须是 JSON 对象")

        def pick(env: str, key: str, default: Any) -> Any:
            """环境变量 > 配置文件 > 默认值。

            注意用 `or`：空字符串视为未设置。
            这样 `COMPANION_EMBED_API_KEY=` 不会覆盖配置文件里的值。
            """
            return os.environ.get(env) or raw.get(key, default)

        def pick_bool(env: str, key: str, default: bool) -> bool:
            v = pick(env, key, default)
            if isinstance(v, bool):
                return v
            return str(v).lower() not in ("0", "false", "no", "off", "")

        cfg = cls(
            app_id=str(pick("QQ_APP_ID", "app_id", "")),
            app_secret=str(pick("QQ_APP_SECRET", "app_secret", "")),
            api_base=str(pick("COMPANION_API_BASE", "api_base", cls.api_base)),
            site_url=str(pick("COMPANION_SITE_URL", "site_url", cls.site_url)),
            db_path=str(pick("COMPANION_DB", "db_path", cls.db_path)),
            bind_host=str(pick("COMPANION_BIND_HOST", "bind_host", cls.bind_host)),
            bind_port=int(pick("COMPANION_BIND_PORT", "bind_port", cls.bind_port)),
            bind_url=str(pick("COMPANION_BIND_URL", "bind_url", cls.bind_url)),
            secret=str(pick("COMPANION_SECRET", "secret", "")),
            embed_enabled=pick_bool("COMPANION_EMBED_ENABLED", "embed_enabled", True),
            embed_api_base=str(
                pick("COMPANION_EMBED_API_BASE", "embed_api_base", cls.embed_api_base)
            ),
            embed_api_key=str(
                pick("COMPANION_EMBED_API_KEY", "embed_api_key", cls.embed_api_key)
            ),
            embed_model=str(
                pick("COMPANION_EMBED_MODEL", "embed_model", cls.embed_model)
            ),
            workers=int(pick("COMPANION_WORKERS", "workers", cls.workers)),
            log_level=str(pick("COMPANION_LOG_LEVEL", "log_level", cls.log_level)),
            extra=raw,
        )

        # AppSecret 支持从文件读，避免出现在 ps 输出里
        secret_file = os.environ.get("QQ_APP_SECRET_FILE")
        if secret_file and os.path.exists(secret_file):
            with open(secret_file, "r", encoding="utf-8") as f:
                cfg.app_secret = f.read().strip()

        return cfg

    def validate(self) -> list[str]:
        """启动前检查。返回错误列表，空表示通过。"""
        errs: list[str] = []
        if not self.app_id:
            errs.append("缺少 QQ_APP_ID（q.qq.com 后台拿）")
        if not self.app_secret:
            errs.append("缺少 QQ_APP_SECRET（或 QQ_APP_SECRET_FILE）")
        if not self.secret:
            errs.append(
                "缺少 COMPANION_SECRET —— 客户 key 会明文存储！"
                "生成方法：python -m companion.cli gen-secret"
            )
        return errs
