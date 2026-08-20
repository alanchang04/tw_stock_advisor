"""
database/connection.py
SQLAlchemy engine + session 管理
"""
from contextlib import contextmanager
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from loguru import logger

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config.settings import DBConfig


_engine = None
_Session = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            DBConfig.url(),
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,      # 自動重連
            echo=False,
        )
    return _engine


def get_session_factory():
    global _Session
    if _Session is None:
        _Session = sessionmaker(bind=get_engine())
    return _Session


@contextmanager
def get_session():
    """
    用法：
        with get_session() as session:
            session.execute(...)
    """
    Session = get_session_factory()
    session = Session()
    try:
        yield session
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"DB session error: {e}")
        raise
    finally:
        session.close()


def test_connection() -> bool:
    """測試 DB 連線是否正常"""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("✅ PostgreSQL 連線成功")
        return True
    except Exception as e:
        logger.error(f"❌ PostgreSQL 連線失敗: {e}")
        return False