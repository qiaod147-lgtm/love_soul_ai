# 部署文档 · Love Soul AI

一个基于 LLM 的多模态恋爱关系咨询 Web,后端 FastAPI + 前端单文件 HTML,
数据走本地 SQLite。

---

## 1. 环境要求

- Python **3.10+** (项目用 `from __future__ import annotations` + PEP 604 union,3.10 起)
- 操作系统:Windows / macOS / Linux 均可
- 内存 ≥ 512 MB,无需 GPU
- 一个**兼容 OpenAI Chat Completions 协议**的 API Key
  (官方 OpenAI、DeepSeek、月之暗面、智谱、Ollama、openrouter 等都行)

---

## 2. 拿到代码

```bash
git clone <your-repo-url> love_soul_ai
cd love_soul_ai
```

> 当前项目结构是 **package 形态**(`__init__.py` + 同级模块),启动必须从
> **父目录** 走 `python -m uvicorn love_soul_ai.web:app`。
> 直接 `cd love_soul_ai && python web.py` 会因为相对 import 失败。

---

## 3. 安装依赖

```bash
# 推荐用虚拟环境
python -m venv .venv

# 激活
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# Windows CMD:
.venv\Scripts\activate.bat
# macOS / Linux:
source .venv/bin/activate

# 装依赖
pip install -r requirements.txt
```

依赖只有 4 个核心包(`openai / fastapi / uvicorn / pydantic`),SQLite / 正则 /
`pathlib` 全部走 stdlib,不引入额外组件。

---

## 4. 配置环境变量

复制示例文件:

```bash
cp .env.example .env
```

`.env` 内容(**不要把真实 key 提交到 git**,`.gitignore` 已包含 `.env`):

```dotenv
# 必填:你的 LLM API Key
OPENAI_API_KEY=sk-your-real-key

# 可选:兼容 OpenAI 协议的端点;不填就走官方 https://api.openai.com/v1
# DeepSeek:    https://api.deepseek.com/v1
# Ollama:     http://localhost:11434/v1
OPENAI_BASE_URL=

# 可选:模型名,默认 gpt-4o-mini(多模态聊天需要支持 vision 的模型,如 gpt-4o)
LOVE_SOUL_MODEL=gpt-4o-mini
```

> **Windows 下让 uvicorn 读 .env 的最简办法**:启动前手动 set,例如
> `setx OPENAI_API_KEY sk-your-key`(新开终端生效),或者在激活虚拟环境后
> 用 `pip install python-dotenv` + 自己改 web.py 入口。本项目不强制依赖
> dotenv,以保持依赖最小。

---

## 5. 启动服务

```bash
# 在项目**父目录**跑(假设仓库目录叫 love_soul_ai)
cd ..
python -m uvicorn love_soul_ai.web:app --host 0.0.0.0 --port 8771
```

启动成功后会看到:

```
INFO:     Started server process [...]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8771 (Press CTRL+C to quit)
```

打开浏览器访问 <http://127.0.0.1:8771> 即可。

### 5.1 只本机访问(默认安全)

把 `--host 0.0.0.0` 改成 `--host 127.0.0.1`,服务只监听本机,更安全。

### 5.2 改端口

`--port 8000` 任意;前端 `web_static/index.html` 里没有写死端口,跟着启动参数走。

### 5.3 跑 CLI(无 Web)

```bash
python -m love_soul_ai.cli            # 默认用户 default
python -m love_soul_ai.cli --user alice
```

支持指令:`/help /clear /history /search /upload /system /stats /exit`。

---

## 6. 反向代理 / 上线(可选)

最简方案:**Nginx + systemd / NSSM(Windows)**。

### 6.1 Nginx 反代

```nginx
server {
    listen 80;
    server_name your.domain.com;

    client_max_body_size 8M;   # 允许上传多图,前端默认单张 ≤ 4MB

    location / {
        proxy_pass         http://127.0.0.1:8771;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 120s;   # LLM 多模态可能慢
    }
}
```

### 6.2 systemd(Linux)

`/etc/systemd/system/love-soul-ai.service`:

```ini
[Unit]
Description=Love Soul AI
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/love_soul_ai
Environment="OPENAI_API_KEY=sk-your-key"
Environment="LOVE_SOUL_MODEL=gpt-4o-mini"
ExecStart=/opt/love_soul_ai/.venv/bin/python -m uvicorn love_soul_ai.web:app --host 127.0.0.1 --port 8771
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now love-soul-ai
sudo journalctl -u love-soul-ai -f
```

### 6.3 Windows 用 NSSM 包成服务

```powershell
nssm install LoveSoulAI "C:\path\to\.venv\Scripts\python.exe" "-m uvicorn love_soul_ai.web:app --host 127.0.0.1 --port 8771"
nssm set LoveSoulAI AppDirectory C:\path\to\parent-dir-of-love_soul_ai
nssm set LoveSoulAI AppEnvironmentExtra OPENAI_API_KEY=sk-your-key
nssm start LoveSoulAI
```

---

## 7. 数据与备份

- 数据库:`~/.love_soul_ai/history.db`(SQLite,WAL 模式)
- 包含两个表:
  - `messages` —— 全量 user/assistant 消息,**含历史图片**(data URI,base64)
  - `summaries` —— 早期对话的滚动摘要(LLM 上下文折叠用)
- 单库即全部状态,无外部依赖。**备份 = 拷这一个文件**:

```bash
# Linux / macOS
cp ~/.love_soul_ai/history.db backup-$(date +%F).db

# Windows PowerShell
Copy-Item $env:USERPROFILE\.love_soul_ai\history.db backup-$(Get-Date -Format yyyy-MM-dd).db
```

> ⚠️ 拷文件前先停服,或用 SQLite 官方 `.backup` 命令在线备份,
> 避免 WAL 模式下漏写。

### 清空历史

- Web 侧边栏 → "🗑 清空历史"
- 或 API:`POST /api/clear {"user_id": "default"}`
- 或直接删库:`rm ~/.love_soul_ai/history.db` (下次启动自动重建)

---

## 8. 自检

不调 LLM,验证 storage / upload / summarize 纯逻辑:

```bash
# 在项目**父目录**跑
python -m love_soul_ai._selftest
```

应输出 `ALL OK`。

---

## 9. 常见问题

**Q1. 启动报 `ModuleNotFoundError: No module named 'love_soul_ai'`**
A: 你在仓库目录里直接 `python -m uvicorn love_soul_ai.web:app` 了。
必须 `cd ..` 回到**父目录**再跑,因为模块名是 `love_soul_ai.xxx`,Python
import 路径要能解析到这一层。

**Q2. 报 `RuntimeError: 需要安装 openai SDK: pip install openai`**
A: 没装依赖。`pip install -r requirements.txt`。

**Q3. 报 `OPENAI_API_KEY not set`**
A: 启动前 export / setx,或写进 systemd / NSSM 的环境变量配置。
**不要**再硬编码到 `run_love_soul_ai.py`,会被推到 git 公开仓库。

**Q4. 上下文太长报错 / 摘要触发不了**
A: 默认 `CONTEXT_WINDOW_TURNS = 30`,大多数 8k+ 模型够用。
真要长上下文,改 `summarize.py` 的常量,或在 storage 层加 token 估算截断。

**Q5. 历史里的图片不显示**
A: 旧版本(< 0.2)库里没 `images_json` 列;升级后第一次启动会自检补列。
若仍异常,看启动日志里 `ALTER TABLE` 是否执行成功。

**Q6. 怎样支持企业内网 / 私有 LLM**
A: 把 `OPENAI_BASE_URL` 指向你们的网关,例如 `http://internal-llm/v1`,
`LOVE_SOUL_MODEL` 改成该网关注册的模型名。其它代码不用动。

---

## 10. 安全提示

- 仓库里**永远不要**提交真实 API Key。`.gitignore` 已屏蔽 `.env`,
  但 git 历史一旦泄漏就救不回来 —— 推前先 `git status` 确认。
- 如果不小心把 key 推上去了,立刻去对应平台**吊销旧 key、签发新 key**,
  单纯 `git rm` + force push 救不了(已经被 fork / 缓存)。
- 公网部署务必加 Nginx 反代 + HTTPS,不要直接 `0.0.0.0` 暴露 8771。
- 聊天记录属于敏感隐私,生产环境建议:
  1. 定期备份 + 加密存储
  2. 加用户登录(`/api/*` 加鉴权中间件)
  3. 关闭 `/api/upload/chat` 的图片(避免违规内容落地)

---

## 11. 目录速查

```
love_soul_ai/                  # ← 本仓库(package 根)
├── __init__.py                # 包入口,version
├── cli.py                     # 命令行 REPL 入口
├── web.py                     # FastAPI 应用 + 路由
├── llm.py                     # OpenAI 协议调用层(单/多模态)
├── storage.py                 # SQLite 持久化 + FTS5 检索
├── summarize.py               # 滚动摘要(LLM 上下文折叠)
├── memory.py                  # 进程内 Session(CLI 用)
├── persona.py                 # 系统提示词 / 人格
├── upload.py                  # 聊天记录解析 + 切段
├── _selftest.py               # 不调 LLM 的自检脚本
├── run_love_soul_ai.py        # 占位启动脚本(推荐用 -m 启动)
├── web_static/
│   └── index.html             # 前端单文件
├── mcp_wechat/                # 微信 MCP 接入预留口(暂无代码)
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
└── DEPLOY.md                  # ← 本文件
```
