"""LocalLabel application entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

if __package__ in {None, ""}:
    # Preserve direct execution with `python src/main.py`.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src.config import STATIC_DIR
    from src.database import engine
    from src.models import Base
    from src.routes import router
    from src.services import migrate_legacy_library, migrate_schema
else:
    from .config import STATIC_DIR
    from .database import engine
    from .models import Base
    from .routes import router
    from .services import migrate_legacy_library, migrate_schema


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    migrate_schema()
    migrate_legacy_library()
    yield


app = FastAPI(title="LocalLabel", version="0.3.0", lifespan=lifespan)
app.include_router(router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
