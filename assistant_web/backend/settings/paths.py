"""프로젝트와 실행 데이터 경로. JSON이나 네트워크를 읽지 않는다."""
import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = BASE_DIR / "static"
APP_DIR = BASE_DIR.parent
WORKSPACE_DIR = APP_DIR.parent
CONFIG_DIR = APP_DIR / "config"
RUNTIME_DIR = Path(os.getenv("ASSISTANT_RUNTIME_DIR", str(APP_DIR / "runtime"))).expanduser()
RUNTIME_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
try:
    RUNTIME_DIR.chmod(0o700)
except OSError:
    pass
CHROMA_DB_PATH = Path(
    os.getenv(
        "CHROMA_DB_PATH",
        str(WORKSPACE_DIR / "data" / "mantis_chroma_db" / "chroma.sqlite3"),
    )
).expanduser()
CHROMA_PERSIST_DIR = CHROMA_DB_PATH.parent
MANTIS_INDEX_DB_PATH = Path(
    os.getenv(
        "MANTIS_INDEX_DB_PATH",
        str(CHROMA_PERSIST_DIR / "mantis_index.sqlite3"),
    )
).expanduser()
AUDIT_DB_PATH = Path(
    os.getenv("AUDIT_DB_PATH", str(RUNTIME_DIR / "assistant_audit.sqlite3"))
).expanduser()
