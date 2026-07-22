"""FastAPI 애플리케이션 조립 지점."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.config import APP_DIR, STATIC_DIR
from backend.environment import load_dotenv


load_dotenv(APP_DIR / ".env")

from backend.api.chat import router as chat_router  # noqa: E402
from backend.api.notion import router as notion_router  # noqa: E402
from backend.api.operations import router as operations_router  # noqa: E402
from backend.api.quality import router as quality_router  # noqa: E402
from backend.api.search import router as search_router  # noqa: E402


def create_app() -> FastAPI:
    application = FastAPI(title="컨텐츠서비스 AI 비서", version="0.1.0")
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:8787", "http://127.0.0.1:8787"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(search_router)
    application.include_router(notion_router)
    application.include_router(quality_router)
    application.include_router(chat_router)
    application.include_router(operations_router)
    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return application


app = create_app()
