"""前端构建产物与接口同源提供：页面、`/api` 与 SSE 共用一个地址，对外只需转发一个端口。"""

from __future__ import annotations

from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles

from server.config import REPO_ROOT

WEB_DIST = REPO_ROOT / "web" / "dist"


class SpaFiles(StaticFiles):
    """找不到的页面路径回退到 index.html，交给前端路由；`/api` 下的未知路径仍是 404。"""

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except HTTPException as error:
            if error.status_code != 404 or path == "api" or path.startswith("api/"):
                raise
            return await super().get_response("index.html", scope)
