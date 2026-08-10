from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import get_settings
from .iiko.client import IikoCloudClient
from .routers import auth, profile


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.iiko_client = IikoCloudClient(settings)
    yield
    await app.state.iiko_client.aclose()


app = FastAPI(title="Маковка API", version="0.1.0", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(profile.router)


@app.get("/health")
async def health():
    settings = get_settings()
    return {"status": "ok", "iiko_env": settings.iiko_env, "otp_mock_mode": settings.otp_mock_mode}
