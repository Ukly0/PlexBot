# cinebot/db/repositories.py
"""
Data access layer on SQLAlchemy 2.x
- Idempotent: uses get_or_create / upsert honoring model uniqueness.
- Focused on operations needed by the scanner and the bot.

Expected tables (store/models.py):
- Library(id, name*, type, root*, created_at)
- Show(id, title, slug, kind, year?, tmdb_id?, tvdb_id?, imdb_id?)
  * UNIQUE(slug, kind)
- Season(id, show_id, library_id, number, path)
  * UNIQUE(show_id, library_id, number)
- Episode(id, season_id, number?, file_path, size?, hash?, message_link?)
  * UNIQUE(season_id, file_path)
"""

from __future__ import annotations
import enum
import re
from pathlib import Path
from typing import Optional, Tuple, List, Iterable, TypeVar
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from fs.scanner import season_from_dirname, parse_season_episode_from_filename, safe_unicode
from .models import Library, LibraryType, Show, Season, Episode  # ChatState is imported on-demand

T = TypeVar("T")
Page = Tuple[List[T], int]  # (items, total)


# -------------------------------
# Helpers
# ----------------------------

def _paginate(stmt, page: int, per_page: int):
    page = max(1, int(page or 1))
    per_page = max(1, int(per_page or 20))
    total = None
    # total with subquery so we don't mutate the original stmt
    total_stmt = select(func.count()).select_from(stmt.subquery())
    return page, per_page, total_stmt


def _show_and_season_from_path(file_path: Path, lib_root: Path) -> Tuple[str, Optional[int], Optional[int]]:
    """
    Derive show_name, season_number, and year from a path:
    - show_name = first segment under root
    - season_number = from season folder or filename
    - year = if show name looks like 'Name(YYYY)'
    """
    rel = file_path.relative_to(lib_root)
    parts = rel.parts
    if len(parts) < 1:
        raise ValueError(f"Unexpected path under {lib_root}: {file_path}")

    raw_show_name = safe_unicode(parts[0])
    # Extract year if format 'Name(YYYY)'
    m = re.match(r"^(.*)\((\d{4})\)$", raw_show_name)
    if m:
        show_name = m.group(1).strip()
        year = int(m.group(2))
    else:
        show_name = raw_show_name
        year = None

    # season_number detection
    season_number = None
    if len(parts) >= 2:
        season_number = season_from_dirname(parts[1])
    if season_number is None:
        s_from_name, _ = parse_season_episode_from_filename(file_path.name)
        if s_from_name is not None:
            season_number = s_from_name
    if season_number is None:
        season_number = 1

    return show_name, season_number, year


# ------------------------------
# Library
# ----------------------------

class LibraryRepo:
    def __init__(self, s: Session):
        self.s = s

    def by_id(self, lib_id: int) -> Optional[Library]:
        return self.s.get(Library, lib_id)

    def by_name(self, name: str) -> Optional[Library]:
        return self.s.scalars(select(Library).where(Library.name == name)).first()

    def list_all(self) -> List[Library]:
        return self.s.scalars(select(Library).order_by(Library.name)).all()

    def list_by_type(self, t: LibraryType) -> List[Library]:
        return self.s.scalars(select(Library).where(Library.type == t).order_by(Library.name)).all()

    def upsert_from_cfg(self, name: str, t: LibraryType, root: str) -> Library:
        """
        Insert a Library if it does not exist by name or root. If it exists, return it unchanged.
        """
        q = select(Library).where((Library.name == name) | (Library.root == root))
        obj = self.s.scalars(q).first()
        if obj:
            return obj
        obj = Library(name=name, type=t, root=root)
        self.s.add(obj)
        self.s.flush()
        return obj


# ----------------------------
# Show
# ----------------------------

class ShowRepo:
    def __init__(self, s: Session):
        self.s = s

    def by_id(self, show_id: int) -> Optional[Show]:
        return self.s.get(Show, show_id)

    def by_slug_kind(self, slug, kind):
    # Convierte el Enum a string si es necesario
        if isinstance(kind, enum.Enum):
            kind = kind.value
        q = select(Show).where(Show.slug == slug, Show.kind == kind)
        return self.s.scalars(q).first()

    def get_or_create(
        self,
        title: str,
        slug: str,
        kind: LibraryType,
        year: Optional[int] = None,
    ) -> Show:
        obj = self.by_slug_kind(slug, kind)
        if obj:
            # If year differs and new year is provided, update it (optionally sync path elsewhere)
            if year is not None and obj.year != year:
                obj.year = year
                self.s.flush()
            return obj
        if isinstance(kind, enum.Enum):
            kind = kind.value
        obj = Show(title=title, slug=slug, kind=kind, year=year)
        self.s.add(obj)
        self.s.flush()
        return obj

    def page_by_kind(
        self,
        kind: LibraryType,
        page: int = 1,
        per_page: int = 20,
        q: Optional[str] = None,
    ) -> Page[Show]:
        stmt = select(Show).where(Show.kind == kind)
        if q:
            ilike = f"%{q.lower()}%"
            stmt = stmt.where(func.lower(Show.title).like(ilike))
        stmt = stmt.order_by(Show.title)

        page, per_page, total_stmt = _paginate(stmt, page, per_page)
        total = self.s.scalar(total_stmt) or 0
        items = self.s.scalars(stmt.offset((page - 1) * per_page).limit(per_page)).all()
        return items, total

    def search_titles(
        self,
        kind: LibraryType,
        q: str,
        limit: int = 20,
    ) -> List[Show]:
        ilike = f"%{q.lower()}%"
        stmt = (
            select(Show)
            .where(Show.kind == kind, func.lower(Show.title).like(ilike))
            .order_by(Show.title)
            .limit(limit)
        )
        return self.s.scalars(stmt).all()


# ----------------------------
# Season
# ----------------------------

class SeasonRepo:
    def __init__(self, s: Session):
        self.s = s

    def by_id(self, season_id: int) -> Optional[Season]:
        return self.s.get(Season, season_id)

    def get(
        self,
        show_id: int,
        library_id: int,
        number: int,
    ) -> Optional[Season]:
        q = select(Season).where(
            Season.show_id == show_id,
            Season.library_id == library_id,
            Season.number == number,
        )
        return self.s.scalars(q).first()

    def upsert(
        self,
        show_id: int,
        library_id: int,
        number: int,
        path: str,
    ) -> Season:
        obj = self.get(show_id, library_id, number)
        if obj:
            # If you need to sync path, update here (optional)
            return obj
        obj = Season(show_id=show_id, library_id=library_id, number=number, path=path)
        self.s.add(obj)
        self.s.flush()
        return obj

    def list_by_show(self, show_id: int) -> List[Season]:
        stmt = select(Season).where(Season.show_id == show_id).order_by(Season.number)
        return self.s.scalars(stmt).all()

    def list_by_library_and_show(self, library_id: int, show_id: int) -> List[Season]:
        stmt = (
            select(Season)
            .where(Season.library_id == library_id, Season.show_id == show_id)
            .order_by(Season.number)
        )
        return self.s.scalars(stmt).all()


# ----------------------------
# Episode
# ----------------------------

class EpisodeRepo:
    def __init__(self, s: Session):
        self.s = s

    def list_by_season(self, season_id: int) -> List[Episode]:
        stmt = select(Episode).where(Episode.season_id == season_id).order_by(Episode.number.nulls_last())
        return self.s.scalars(stmt).all()

    def get(self, season_id: int, file_path: str) -> Optional[Episode]:
        q = select(Episode).where(Episode.season_id == season_id, Episode.file_path == file_path)
        return self.s.scalars(q).first()

    def upsert(
        self,
        season_id: int,
        file_path: str,
        number: Optional[int] = None,
        size: Optional[int] = None,
        file_hash: Optional[str] = None,
        message_link: Optional[str] = None,
    ) -> Episode:
        obj = self.get(season_id, file_path)
        if obj:
            return obj
        obj = Episode(
            season_id=season_id,
            file_path=file_path,
            number=number,
            size=size,
            hash=file_hash,
            message_link=message_link,
        )
        self.s.add(obj)
        self.s.flush()
        return obj


# ----------------------------
# ChatState
# ----------------------------

class ChatStateRepo:
    def __init__(self, s: Session):
        self.s = s

    def get(self, chat_id: str):
        from .models import ChatState  # import diferido para no romper imports circulares
        return self.s.get(ChatState, chat_id)

    def set_state(self, chat_id: str, **kwargs):
        """
        set_state(chat_id, current_show_id=..., current_library_id=..., current_season=...)
        """
        from .models import ChatState
        st = self.s.get(ChatState, chat_id)
        if not st:
            st = ChatState(chat_id=chat_id)
            self.s.add(st)
            self.s.flush()
        for k, v in kwargs.items():
            if hasattr(st, k):
                setattr(st, k, v)
        self.s.flush()
        return st
