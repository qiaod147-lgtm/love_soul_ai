# Love Soul AI · 恋爱关系导师

> 🤖 **Love Soul AI** — 一个跑在本地/自己服务器上的多模态恋爱关系咨询 Web。
> 支持文字+图片聊天、贴图(粘贴截图直接发)、上传微信/QQ/iMessage 聊天记录做沟通建议 / 关系复盘。
> 人格设定强调"先共情后建议、拒绝 PUA"、危险信号识别。
> 后端 FastAPI + SQLite(全量消息持久化 + FTS5 关键词检索 + 滚动摘要折叠上下文),前端单 HTML,兼容任何 OpenAI 协议端点(OpenAI / DeepSeek / Ollama …)。

基于 LLM 的多模态情感咨询 Web:
- 普通聊天(支持贴图)
- 上传聊天记录做关系分析(支持贴图)

## 部署

详见 [DEPLOY.md](./DEPLOY.md)(环境变量、Nginx 反代、systemd / NSSM、备份、常见问题)。

## 启动

```bash
# 端口 8771,后台跑
python run_love_soul_ai.py
```

环境变量:`OPENAI_API_KEY`(必填)、`OPENAI_BASE_URL`(可选,兼容 DeepSeek 等)、`LOVE_SOUL_MODEL`(可选,默认 gpt-4o-mini)。

数据存 `~/.love_soul_ai/history.db`(SQLite,自动迁移)。

## 用法

**普通聊天**:输入框直接打字,回车发送。
**输入框贴图**:直接 Ctrl+V 粘贴截图,缩略图出现在输入框上方,× 可移除,回车**和文字一起发**。粘贴的图会作为多模态消息发给 LLM。
**聊天分析**:左侧"上传聊天记录",填聊天记录文本 + 选"我是谁"(可选)+ 拖截图 + 选分析模式 → "分析"。**默认保存到历史**。

## 历史规则

- 所有消息(普通聊天 + 聊天分析)**都**进历史。
- 用户消息**带图入库**(data URI 存 `messages.images_json`),历史加载时**展示图**。
- 助手消息**不带图**(回复是纯文字)。
- 全文搜索(FTS5)只搜 content 文字,不搜图片 base64。
- **当前轮**用户消息的图会发给 LLM 多模态看;**历史轮**的图只展示不回传(避免 token 爆)。

## API 端点

| 路径 | 方法 | 用途 |
|------|------|------|
| `/api/chat` | POST | 普通聊天,Form(multipart)接 `message` + `images` |
| `/api/upload/chat` | POST | 聊天分析,Form 接 `text` + `images` + `mode` + `me` + `save_to_history`(默认 true) |
| `/api/history` | POST | 取历史,JSON 接 `user_id` + `limit`,返回 messages(含 images 字段) |
| `/api/search` | POST | 跨历史关键词搜索 |
| `/api/clear` | POST | 清空历史 |
| `/api/clear_user` | POST | 清空指定用户 |
| `/api/health` | GET | 健康检查 |

## 限制

- 单张图 ≤ 256KB(MIME: jpg/png/gif/webp)
- 历史轮图片不回传 LLM(只展示)。需要老图也喂 LLM,改 `llm.py` `chat_with_images` 的 msgs 构造段,限最近 N 轮。
- 上传聊天分析走 `chat_with_context`(独立任务 prompt),不与普通聊天上下文合并。
