"""启动 love_soul_ai web 服务,固定 8771。

⚠️ 这个脚本只是"误用示例"占位 —— 实际启动请走 -m:
    cd ..
    python -m uvicorn love_soul_ai.web:app --port 8771

原因:web.py 等模块用相对 import (from .llm import ...),只能
作为 package 子模块加载,不能直接 python run_love_soul_ai.py。

环境变量(启动前请 export 或写入 .env,不要再硬编码到脚本里):
    OPENAI_API_KEY     必填,你的 API Key
    OPENAI_BASE_URL    可选,兼容端点(默认官方 OpenAI)
    LOVE_SOUL_MODEL    可选,模型名(默认 gpt-4o-mini)
"""
import os
import sys

import uvicorn


def main() -> int:
    # ponytail: 启动前只校验 key 在不在,不再预设具体值,避免泄漏或绑死供应商
    if not os.environ.get("OPENAI_API_KEY"):
        print("[错误] 未设置 OPENAI_API_KEY 环境变量。", file=sys.stderr)
        print("       请先 export OPENAI_API_KEY=sk-... 或写入 .env 文件。", file=sys.stderr)
        return 1

    uvicorn.run(
        "love_soul_ai.web:app",
        host="127.0.0.1",
        port=8771,
        reload=False,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
