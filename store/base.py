# store/base.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Declarative Base única para todos los modelos
Base = declarative_base()

def make_engine(db_url: str, *, echo: bool = False):
    """
    Crea el engine SQLAlchemy.
    Ejemplos de db_url:
      - sqlite:///cinebot.db
      - postgresql+psycopg://user:pass@localhost:5432/cinebot
    """
    return create_engine(db_url, future=True, echo=echo)

def make_session_factory(engine):
    """
    Devuelve un sessionmaker configurado para sesiones 'cortas'.
    """
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
