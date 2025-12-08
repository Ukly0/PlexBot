# store/base.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Declarative Base shared by all models
Base = declarative_base()

def make_engine(db_url: str, *, echo: bool = False):
    """
    Create SQLAlchemy engine.
    Example db_url:
      - sqlite:///plexbot.db
      - postgresql+psycopg://user:pass@localhost:5432/plexbot
    """
    return create_engine(db_url, future=True, echo=echo)

def make_session_factory(engine):
    """
    Return a sessionmaker configured for short-lived sessions.
    """
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
