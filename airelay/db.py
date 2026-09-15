"""数据库：SQLite + SQLAlchemy 异步引擎。

单机零运维（方案 4.1 的选型理由），启用 WAL 以保证流式写入时不阻塞读，
并设置 busy_timeout 规避「database is locked」。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .models import Base

log = logging.getLogger(__name__)

DB_FILENAME = "airelay.db"


def build_engine(db_path: Path, *, echo: bool = False) -> AsyncEngine:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    engine = create_async_engine(
        url,
        echo=echo,
        future=True,
        pool_pre_ping=True,
        connect_args={"timeout": 30, "check_same_thread": False},
    )
    _install_sqlite_pragmas(engine)
    return engine


def _install_sqlite_pragmas(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection, _record):  # pragma: no cover - 由驱动回调
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA temp_store=MEMORY")
        except Exception:  # 某些嵌入式 SQLite 不支持全部 pragma
            log.debug("设置 SQLite pragma 时出现非致命错误", exc_info=True)
        finally:
            cursor.close()


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all 不会给已存在的表补列，老库升级要显式加
        await conn.run_sync(_apply_light_migrations)
    log.debug("数据库表结构已就绪")


# 轻量迁移：SQLite 加列成本很低，且几乎不会失败，够单机工具用。
# 形如 {表名: {列名: "列定义"}}；加过的列再执行是空操作。
LIGHT_MIGRATIONS: dict[str, dict[str, str]] = {
    "api_keys": {
        # 让本地密钥的明文可被取回（平台式密钥管理）；老数据为空，控制台会提示重新生成
        "key_enc": "TEXT DEFAULT ''",
    },
    "channels": {
        # 本地进程托管配置（启动命令 / 自动重启等）
        "lifecycle": "TEXT DEFAULT '{}'",
    },
}


def _apply_light_migrations(connection) -> None:
    from sqlalchemy import text as sql_text

    for table, columns in LIGHT_MIGRATIONS.items():
        existing = {
            row[1] for row in connection.execute(sql_text(f"PRAGMA table_info({table})")).fetchall()
        }
        if not existing:
            continue  # 表还不存在，create_all 已经建好了带列的新表
        for column, ddl in columns.items():
            if column in existing:
                continue
            connection.execute(sql_text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            log.info("数据库升级：%s 增加列 %s", table, column)


async def dispose(engine: AsyncEngine) -> None:
    await engine.dispose()


async def session_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """便捷的异步上下文管理器。"""
    async with factory() as session:
        yield session


async def prune_usage_logs(engine: AsyncEngine, retention_days: int) -> int:
    """清理超过保留期的用量明细与余额快照，返回删除行数。"""
    from datetime import timedelta

    from .models import BalanceSnapshot, UsageLog
    from .timeutil import utcnow
    from sqlalchemy import delete

    if retention_days <= 0:
        return 0
    cutoff = utcnow() - timedelta(days=retention_days)
    removed = 0
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        result = await session.execute(delete(UsageLog).where(UsageLog.ts < cutoff))
        removed += result.rowcount or 0
        result = await session.execute(
            delete(BalanceSnapshot).where(BalanceSnapshot.fetched_at < cutoff)
        )
        removed += result.rowcount or 0
        await session.commit()
    return removed


async def sqlite_version(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        result = await conn.execute(text("select sqlite_version()"))
        return str(result.scalar_one())
