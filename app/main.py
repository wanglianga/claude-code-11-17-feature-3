"""FastAPI 应用入口。"""
from __future__ import annotations

from fastapi import FastAPI

from app import __version__
from app.db import Base, engine
from app.routers import imports, qc, queries, reports, review

# 导入模型以保证 Base.metadata 完整
from app import models  # noqa: F401

app = FastAPI(
    title="实验室原料检验数据治理服务",
    version=__version__,
    description=(
        "接收实验室原始记录（CSV/JSON），关联取样与供货批次，统一计量含义，"
        "以数值/区间/定性三分方式保存结果，按随附业务规则做区间判定，"
        "并提供冲突复核、批次结论与报告批准/撤回留痕；"
        "内置质控样漂移监测（越界=已证实失效 / 趋势=疑似漂移）与受影响报告追查，"
        "报告批准受未结质控影响限制，主管登记校准/复测证据后逐项解除。"
    ),
)

app.include_router(imports.router)
app.include_router(review.router)
app.include_router(reports.router)
app.include_router(queries.router)
app.include_router(qc.router)


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok", "version": __version__}


@app.post("/admin/init-db", tags=["system"])
def init_db():
    """建表（开发/演示用；生产应由迁移工具管理）。"""
    Base.metadata.create_all(bind=engine)
    return {"status": "created", "tables": sorted(Base.metadata.tables.keys())}
