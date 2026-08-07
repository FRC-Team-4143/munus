from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, init_db
from app.routers import portal, admin, slack
from app.services.scheduler import create_scheduler, job_auto_archive_opportunities


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Catch-up run: IntervalTrigger's first fire is a full interval after startup, so
    # without this, opportunities that were already stale before a deploy wouldn't be
    # swept for up to 6h. Cheap and idempotent — only currently-active opportunities are
    # considered — so running it again on every restart is harmless.
    await job_auto_archive_opportunities()
    scheduler = create_scheduler()
    scheduler.start()
    app.state.scheduler = scheduler
    yield
    scheduler.shutdown()


app = FastAPI(title="Munus", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(portal.router)
app.include_router(admin.router)
app.include_router(slack.router)


@app.get("/health")
async def health(db: AsyncSession = Depends(get_db)):
    """Unauthenticated liveness + DB check — polled by Legion's admin dashboard
    System Status panel and available for external uptime monitoring."""
    try:
        await db.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"status": "error", "app": "munus"}, status_code=503)
    return {"status": "ok", "app": "munus"}
