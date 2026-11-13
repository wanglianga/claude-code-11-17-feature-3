"""命令行入口：建表、种子装载、启动服务。

用法：
  python -m app.cli init-db
  python -m app.cli seed [--ref-dir data/reference]
  python -m app.cli run --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import argparse

import uvicorn

from app.db import Base, SessionLocal, engine
from app.models import AuditEvent  # noqa: F401  确保模型已注册
from app.seed import seed_all


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    print(f"已建表：{sorted(Base.metadata.tables.keys())}")


def seed(ref_dir: str) -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        result = seed_all(db, ref_dir)
        db.commit()
        print(f"参考数据装载完成：{result}")
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="实验室检验数据治理服务 CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    p_seed = sub.add_parser("seed")
    p_seed.add_argument("--ref-dir", default="data/reference")
    p_run = sub.add_parser("run")
    p_run.add_argument("--host", default="0.0.0.0")
    p_run.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.cmd == "init-db":
        init_db()
    elif args.cmd == "seed":
        seed(args.ref_dir)
    elif args.cmd == "run":
        uvicorn.run("app.main:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
