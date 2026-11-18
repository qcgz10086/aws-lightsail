import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.database import SessionLocal, init_db
from app.routers import auth, billing, billing_v2, catalog, credentials, instances, metrics, users
from app.seed import seed_admin
from app.services.collector import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    db = SessionLocal()
    try:
        seed_admin(db)
    finally:
        db.close()
    start_scheduler()
    logger.info("Lightsail Manager API 已启动")
    yield
    stop_scheduler()
    logger.info("Lightsail Manager API 已停止")


settings = get_settings()

# R-9：默认关闭 /docs、/redoc、/openapi.json；仅 ENABLE_DOCS=true 时开启。
app = FastAPI(
    title="Lightsail Manager",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
)

# R-8：默认不放行任何跨域来源，只有 CORS_ORIGINS 里明确列出的才放行；
# 凭据只在有明确来源时才允许；方法与请求头收敛到实际用到的范围。
# 生产由 nginx 同源反代，不依赖 CORS。
_cors_origins = settings.cors_origin_list
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=bool(_cors_origins),
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    # R-7：安全响应头。纯 HTTP 部署不加 HSTS（HSTS 只在 HTTPS 下有意义，
    # 且一旦下发无法撤回）；TLS 由反代终止，见 README。
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; frame-ancestors 'none'; base-uri 'self'"
    )
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


app.include_router(auth.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(credentials.router, prefix="/api")
app.include_router(catalog.router, prefix="/api")
app.include_router(instances.router, prefix="/api")
app.include_router(metrics.router, prefix="/api")
app.include_router(billing.router, prefix="/api")
app.include_router(billing_v2.router, prefix="/api")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}
