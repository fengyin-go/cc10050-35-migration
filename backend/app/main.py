"""商品价格监控采集平台 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8020
健康检查：GET /api/health
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import ROUTERS
from app.routers import migration as migration_router
from app.services.metrics_migration import migration_service
from app.store import store


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动即接收/幂等复验站点 K 的旧累计 37，并投影到存量模块。
    migration_service.seed_canonical()
    yield


app = FastAPI(title="商品价格监控采集平台", version="1.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪。"""
    return {"ok": True, "app": settings.app_name, "modules": len(store.module_names())}


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()


# 采集量迁移核验接口：主前缀 + 归属到 collect_metrics 的别名前缀。
app.include_router(migration_router.router, prefix="/api/metrics-migration")
app.include_router(
    migration_router.router, prefix="/api/collect_metrics/migration"
)

# 资源路由含 /api/{module}，必须放在 health / overview 之后注册，
# 否则固定路径会被动态路径抢先匹配。
for module in ROUTERS:
    app.include_router(module.router)
