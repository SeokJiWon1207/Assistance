"""FastAPI 애플리케이션 조립 지점."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.environment import load_dotenv


APP_DIR = Path(__file__).resolve().parent.parent
load_dotenv(APP_DIR / ".env")

from backend.auth import authentication_middleware, init_users_db  # noqa: E402
from backend.audit import init_audit_db, prune_audit_logs  # noqa: E402
from backend.api.chat import router as chat_router  # noqa: E402
from backend.api.auth import router as auth_router  # noqa: E402
from backend.api.notion import router as notion_router  # noqa: E402
from backend.api.operations import router as operations_router  # noqa: E402
from backend.api.playbooks import router as playbooks_router  # noqa: E402
from backend.api.quality import router as quality_router  # noqa: E402
from backend.api.search import router as search_router  # noqa: E402
from backend.api.templates import router as templates_router  # noqa: E402
from backend.api.workspace import router as workspace_router  # noqa: E402
from backend.config import STATIC_DIR  # noqa: E402
from backend.startup_validation import validate_runtime_security  # noqa: E402
from backend.workspace import init_workspace_db  # noqa: E402


@asynccontextmanager
async def lifespan(_application: FastAPI):
    validate_runtime_security()
    init_users_db()
    init_workspace_db()
    init_audit_db()
    prune_audit_logs()
    yield


class CacheRevalidatedStaticFiles(StaticFiles):
    """정적 파일에 항상 재검증을 요구한다.

    Cache-Control 이 없으면 브라우저가 휴리스틱 캐싱을 적용해, 배포 후에도
    옛 CSS/JS 를 그대로 쓰는 일이 생긴다. index.html 이 ?v= 로 버전을 붙이는
    파일은 일부뿐이고 modules/ 아래 모듈은 버전이 없어 특히 취약하다.
    no-cache 는 캐시를 막는 게 아니라 매번 ETag 로 확인하게 하므로,
    바뀌지 않았다면 304 로 끝나 전송량은 늘지 않는다.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


def create_app() -> FastAPI:
    application = FastAPI(
        title="컨텐츠서비스 AI 비서",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.middleware("http")(authentication_middleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:8787", "http://127.0.0.1:8787"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(auth_router)
    application.include_router(workspace_router)
    application.include_router(search_router)
    application.include_router(templates_router)
    application.include_router(notion_router)
    application.include_router(quality_router)
    application.include_router(chat_router)
    application.include_router(operations_router)
    application.include_router(playbooks_router)
    application.mount("/static", CacheRevalidatedStaticFiles(directory=STATIC_DIR), name="static")
    return application


app = create_app()
