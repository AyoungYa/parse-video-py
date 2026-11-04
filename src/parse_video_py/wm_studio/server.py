# -*- coding: utf-8 -*-
"""wm-studio 独立启动入口（不启动视频解析服务，只跑调参台）。

用法:
    uv run wm-studio                     # 默认 127.0.0.1:8010
    uv run wm-studio --port 8020 --host 0.0.0.0

也可以直接走主服务的 /wm（`uv run python -m parse_video_py.main`），
两者共用同一套路由；独立入口只是省掉解析服务的初始化与鉴权干扰。
"""

import argparse

import uvicorn
from fastapi import FastAPI

from parse_video_py.wm_studio.api import router


def build_app() -> FastAPI:
    app = FastAPI(title="去水印调参台", docs_url="/docs")
    app.include_router(router)
    return app


def main() -> int:
    ap = argparse.ArgumentParser(description="去水印调参台")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--reload", action="store_true", help="改代码自动重载（开发用）")
    args = ap.parse_args()

    uvicorn.run(
        "parse_video_py.wm_studio.server:build_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
