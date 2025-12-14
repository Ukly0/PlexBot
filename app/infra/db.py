from contextlib import contextmanager
import logging

from sqlalchemy.exc import OperationalError

from config.settings import load_settings
from store.base import Base, make_engine, make_session_factory

# Ensure models are registered with Base.metadata
import store.models  # noqa: F401


def _seed_libraries(session, st):
    """Insert libraries from config if none exist yet."""
    try:
        from store.repos import LibraryRepo
        from store.models import LibraryType
    except Exception as e:
        logging.warning("Could not import repos for seeding: %s", e)
        return
    repo = LibraryRepo(session)
    if repo.list_all():
        return
    inserted = 0
    for cfg in st.libraries:
        try:
            repo.upsert_from_cfg(cfg.name, LibraryType(cfg.type), cfg.root)
            inserted += 1
        except Exception as e:
            logging.warning("Could not insert library %s (%s): %s", cfg.name, cfg.root, e)
    if inserted:
        session.commit()
        logging.info("Seeded %s libraries from config.", inserted)


@contextmanager
def get_session():
    """
    Yield a DB session, auto-creating schema (and seeding libraries if empty) when missing.
    """
    st = load_settings()
    engine = make_engine(st.db_url)
    SessionLocal = make_session_factory(engine)
    session = SessionLocal()
    try:
        try:
            Base.metadata.create_all(bind=engine)
            _seed_libraries(session, st)
        except OperationalError as e:
            logging.error("DB initialization failed: %s", e)
        yield session
    finally:
        session.close()
