from contextlib import contextmanager

from config.settings import load_settings
from store.base import make_engine, make_session_factory

@contextmanager
def get_session():
    st = load_settings()
    engine = make_engine(st.db_url)
    SessionLocal = make_session_factory(engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
