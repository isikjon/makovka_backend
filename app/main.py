from contextlib import asynccontextmanager

from fastapi import FastAPI

from .admin.web import admin_app
from .config import get_settings
from .iiko.client import IikoCloudClient
from .routers import auth, content, loyalty, media, profile, referral
from .sms.client import SmsRuClient


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.iiko_client = IikoCloudClient(settings)
    app.state.sms_client = SmsRuClient(settings)
    admin_app.state.iiko_client = app.state.iiko_client
    yield
    await app.state.iiko_client.aclose()
    await app.state.sms_client.aclose()


app = FastAPI(title="Пекарня API", version="0.1.0", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(profile.router)
app.include_router(loyalty.router)
app.include_router(content.router)
app.include_router(referral.router)
app.include_router(media.router)
app.mount("/admin", admin_app)


@app.get("/health")
async def health():
    settings = get_settings()
    return {"status": "ok", "iiko_env": settings.iiko_env, "otp_mock_mode": settings.otp_mock_mode}
