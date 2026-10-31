"""网页绑定页。

为什么不让客户在 QQ 里发 key：
    QQ 聊天记录永久留存，等于把密钥抄送给腾讯和任何拿到记录的人。
    网页绑定 + 一次性码把 key 的暴露面降到最低。
"""
from __future__ import annotations

import html
import json
import time
from typing import Any

import aiohttp
from aiohttp import web

from .. import text as T
from ..db.store import Store
from ..llm.provider import check_key


def _json(data: dict, status: int = 200) -> web.Response:
    """统一 JSON 响应：中文不转义，便于排查。"""
    return web.Response(
        text=json.dumps(data, ensure_ascii=False),
        status=status,
        content_type="application/json",
        charset="utf-8",
    )

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>绑定 API Key</title>
<style>
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#0f1115;color:#e6e8eb;font:15px/1.6 -apple-system,BlinkMacSystemFont,
       "Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;padding:20px}
  .card{width:100%;max-width:440px;background:#171a21;border:1px solid #252a34;
        border-radius:8px;padding:28px}
  h1{margin:0 0 6px;font-size:19px;font-weight:600}
  .sub{margin:0 0 22px;color:#8b93a1;font-size:13px}
  label{display:block;margin-bottom:6px;font-size:13px;color:#a8b0bd}
  input{width:100%;padding:11px 13px;background:#0f1115;border:1px solid #2d3340;
        border-radius:6px;color:#e6e8eb;font-size:14px;font-family:ui-monospace,monospace}
  input:focus{outline:none;border-color:#3d7eff}
  button{width:100%;margin-top:16px;padding:12px;background:#3d7eff;color:#fff;border:0;
         border-radius:6px;font-size:15px;font-weight:500;cursor:pointer}
  button:disabled{background:#2a3040;color:#666;cursor:not-allowed}
  .msg{margin-top:16px;padding:11px 13px;border-radius:6px;font-size:13px;display:none}
  .ok{background:#122c1e;border:1px solid #1d4a31;color:#5ed68a}
  .err{background:#2c1418;border:1px solid #4a1d24;color:#f07080}
  .note{margin-top:18px;padding-top:16px;border-top:1px solid #252a34;
        color:#6b7280;font-size:12px;line-height:1.7}
  .code{font-family:ui-monospace,monospace;color:#3d7eff}

  /* 成功面板：盖住整个卡片，视觉上"事情结束了" */
  .done{display:none;text-align:center;padding:8px 0 4px}
  .done .tick{width:56px;height:56px;margin:0 auto 16px;border-radius:50%;
              background:#122c1e;border:2px solid #2e7d4f;display:flex;
              align-items:center;justify-content:center}
  .done .tick svg{width:28px;height:28px;stroke:#5ed68a;stroke-width:3;
                  fill:none;stroke-linecap:round;stroke-linejoin:round}
  .done h2{margin:0 0 8px;font-size:18px;color:#5ed68a;font-weight:600}
  .done p{margin:0;color:#8b93a1;font-size:13px;line-height:1.7}
  .done .bal{margin-top:14px;padding:10px 13px;background:#0f1115;
             border:1px solid #252a34;border-radius:6px;font-size:13px;color:#a8b0bd}
  .done .bal b{color:#e6e8eb;font-weight:600}
  .closing{margin-top:16px;font-size:12px;color:#6b7280}
  @keyframes pop{from{opacity:0;transform:scale(.94)}to{opacity:1;transform:scale(1)}}
  .done.show{display:block;animation:pop .28s ease-out}
</style>
</head>
<body>
<div class="card">
  <h1>绑定 API Key</h1>
  <p class="sub">绑定后即可开始对话。密钥加密存储，不会出现在聊天记录里。</p>
  <form id="f">
    <label for="k">你的 API Key</label>
    <input id="k" type="password" placeholder="sk-..." autocomplete="off" spellcheck="false">
    <button id="b" type="submit">验证并绑定</button>
  </form>
  <div id="m" class="msg"></div>

  <div id="done" class="done">
    <div class="tick">
      <svg viewBox="0 0 24 24"><polyline points="4,12 10,18 20,6"></polyline></svg>
    </div>
    <h2>绑定成功</h2>
    <p>现在可以回 QQ 聊天了。</p>
    <div class="bal" id="bal"></div>
    <p class="closing" id="closing">这个页面会在 3 秒后自动关闭</p>
  </div>

  <div class="note" id="note">
    绑定码 <span class="code">__CODE__</span>，10 分钟内有效，只能用一次。<br>
    去哪里拿 key：登录中转站 → 令牌 → 新建 → 复制。
  </div>
</div>
<script>
const f=document.getElementById('f'),k=document.getElementById('k'),
      b=document.getElementById('b'),m=document.getElementById('m');
function show(text,ok){m.textContent=text;m.className='msg '+(ok?'ok':'err');}
f.addEventListener('submit',async e=>{
  e.preventDefault();
  const key=k.value.trim();
  if(!key){show('请输入 key',false);return;}
  b.disabled=true;b.textContent='验证中…';
  try{
    const r=await fetch('/bind/api',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({code:__CODEJSON__,key})});
    const d=await r.json();
    if(d.ok){
      k.value='';
      f.style.display='none';
      m.style.display='none';
      document.getElementById('note').style.display='none';
      document.querySelector('h1').textContent='已完成';
      document.querySelector('.sub').textContent='密钥已加密保存。';

      const bal=document.getElementById('bal');
      if(d.unlimited){
        bal.innerHTML='额度：<b>无限</b>';
      }else if(typeof d.balance_cny==='number'){
        bal.innerHTML='当前余额：<b>'+d.balance_cny.toFixed(2)+' 元</b>';
      }else{
        bal.style.display='none';
      }

      const done=document.getElementById('done');
      done.classList.add('show');

      // 倒计时后尝试关闭窗口。
      // 注意：不是我们自己 window.open 出来的标签页，浏览器不允许脚本关闭，
      // 所以失败时要给一个明确的"已成功，可以关掉了"的提示，不能让用户以为没成功。
      let left=3;
      const closing=document.getElementById('closing');
      const timer=setInterval(()=>{
        left-=1;
        if(left>0){
          closing.textContent='这个页面会在 '+left+' 秒后自动关闭';
          return;
        }
        clearInterval(timer);
        window.close();
        // 2 秒后还在，说明浏览器拒绝了关闭
        setTimeout(()=>{
          closing.innerHTML='如果页面没有自动关闭，直接关掉这个标签页即可。<br>回 QQ 发 /model 选择模型。';
        },600);
      },1000);
    }else{
      show(d.message||'绑定失败',false);
      b.disabled=false;b.textContent='验证并绑定';
    }
  }catch(err){
    show('网络错误，请重试',false);
    b.disabled=false;b.textContent='验证并绑定';
  }
});
</script>
</body>
</html>"""


def make_app(
    store: Store,
    session: aiohttp.ClientSession,
    crypto: Any,
    api_base: str,
    log: Any = print,
) -> web.Application:
    app = web.Application()

    async def page(request: web.Request) -> web.Response:
        code = (request.query.get("code") or "").strip().upper()
        if not code:
            return web.Response(text="缺少绑定码", status=400)

        body = (
            PAGE.replace("__CODE__", html.escape(code))
            .replace("__CODEJSON__", json.dumps(code))
        )
        return web.Response(text=body, content_type="text/html", charset="utf-8")

    async def api(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return _json({"ok": False, "message": "请求格式错误"})

        code = str(data.get("code") or "").strip().upper()
        key = str(data.get("key") or "").strip()

        if not code or not key:
            return _json({"ok": False, "message": "参数缺失"})
        if not key.startswith("sk-"):
            return _json(
                {"ok": False, "message": "key 格式不对，应该以 sk- 开头"}
            )

        # ① 先只校验码，不作废 —— key 输错时不能把码烧掉
        info_of_code = store.peek_bind_info(code)
        if info_of_code is None:
            return _json(
                {"ok": False, "message": "绑定码无效或已过期，回 QQ 重新发 /setkey"}
            )
        user_id, kind = info_of_code

        # ② 校验 key
        info = await check_key(session, api_base, key)

        # 额度接口限流/故障时，只要模型列表拿到了，就说明 key 是好的。
        # 实测：/api/usage/token/ 429 而 /v1/models 200 —— 不能因此拒绝绑定。
        if info.unknown and info.available:
            log(f"[bind] 额度未知但 key 可用，放行")
        elif not info.valid:
            return _json({"ok": False, "message": info.error or "key 无效"})
        elif not info.unlimited and info.balance <= 0:
            return _json(
                {"ok": False, "message": "这个 key 额度为零，充点钱再来"}
            )

        # ③ 全部通过才作废码（防并发：UPDATE 带 used_at IS NULL）
        if store.consume_bind_code(code) is None:
            return _json(
                {"ok": False, "message": "绑定码刚被使用，请回 QQ 重新发 /setkey"}
            )

        # 加密入库
        cipher = crypto.encrypt(key)
        if kind == "image":
            # 生图 key：单独存，不碰聊天 key。
            store.set_image_key(
                user_id,
                cipher=cipher,
                suffix=key[-4:],
                api_base=api_base,
            )
            log(f"[bind] user={user_id} image-key fp={T.fingerprint(key)}")
            return _json(
                {
                    "ok": True,
                    "message": "生图 key 绑定成功",
                    "models": len(info.available or []),
                    "kind": "image",
                }
            )

        store.set_key(
            user_id,
            cipher=cipher,
            fingerprint=T.fingerprint(key),
            suffix=key[-4:],
            api_base=api_base,
        )
        store.update_check(
            user_id,
            valid=True,
            balance=info.balance,
            unlimited=info.unlimited,
        )
        store.set_user_status(user_id, "active")

        log(f"[bind] user={user_id} fp={T.fingerprint(key)}")

        # quota → 元：quota_per_unit = 500000 表示 500000 quota = $1
        # 汇率按 7.2 估算，仅用于展示
        balance_cny = None if info.unlimited else round(info.balance / 500000 * 7.2, 2)

        return _json(
            {
                "ok": True,
                "message": "绑定成功",
                "balance": info.balance,
                "balance_cny": balance_cny,
                "unlimited": info.unlimited,
                "models": len(info.available or []),
            }
        )

    async def export_page(request: web.Request) -> web.Response:
        """数据导出：一次性码换取 JSON 文件。

        合规要求（QQ 运营规范 5.10）：用户必须能拿到自己的数据副本。
        """
        code = (request.query.get("code") or "").strip().upper()
        if not code:
            return web.Response(text="缺少导出码", status=400)

        user_id = store.consume_export_code(code)
        if user_id is None:
            return web.Response(
                text="导出码无效或已过期。回 QQ 重新发 /export",
                status=403,
                content_type="text/plain",
                charset="utf-8",
            )

        cur = store.conn.execute(
            """SELECT role, content, created_at FROM messages
               WHERE user_id=? ORDER BY id""",
            (user_id,),
        )
        msgs = [
            {
                "role": r["role"],
                "content": r["content"],
                "time": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(int(r["created_at"]))
                ),
            }
            for r in cur.fetchall()
        ]

        facts = store.get_facts(user_id, "")
        cfg = store.get_config(user_id)
        personas = store.list_personas(user_id)
        usage = store.get_usage(user_id)

        payload = {
            "导出时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "说明": "这是你在陪伴机器人里的全部数据。key 不会包含在导出内容里。",
            "配置": {
                "模型": cfg.get("model"),
                "人设": cfg.get("custom_persona") or "（默认）",
                "语气": cfg.get("modifier_id"),
            },
            "用量": usage,
            "人设": [
                {"名字": p["name"], "描述": p["content"]} for p in personas
            ],
            "记忆": [{"类别": f["key"], "内容": f["value"]} for f in facts],
            "对话": msgs,
        }

        body = json.dumps(payload, ensure_ascii=False, indent=2)
        filename = f"companion-data-{user_id}.json"
        return web.Response(
            body=body,
            content_type="application/json",
            charset="utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    app.router.add_get("/", page)
    app.router.add_get("/bind", page)
    app.router.add_post("/bind/api", api)
    app.router.add_get("/export", export_page)

    return app
