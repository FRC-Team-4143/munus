from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

from app.database import init_db
from app.routers import portal, admin, slack
from app.services.scheduler import create_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
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
