from sqlalchemy import (
    Column,
    Integer,
    String,
    Enum as SAEnum,
    ForeignKey,
    UniqueConstraint,
    DateTime,
    BigInteger,
    CheckConstraint,
    Index,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import enum

from .base import Base

# ------------------------------------------------------------------
# Enums
# ------------------------------------------------------------------

class LibraryType(enum.Enum):
    movies = "movies"
    series = "series"
    anime = "anime"
    documentary = "documentary"
    docuseries = "docuseries"


class Library (Base):

    __tablename__ = "libraries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    type = Column(SAEnum(LibraryType), nullable=False)
    root = Column(String(255), unique=True ,nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    def __repr__(self):
        return f"<Library(id={self.id}, name={self.name}, type={self.type}, root={self.root})>"

class Show (Base):

    __tablename__ = "shows"
    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    slug = Column (String, nullable=False)
    kind = Column(String, nullable=False)
    year = Column(Integer)
    tmdb_id = Column(Integer)
    tvdb_id = Column(Integer)
    imdb_id = Column(String(15))

    __table_args__ = (
        UniqueConstraint("slug", "kind", name="uq_slug_kind"),
        Index("ix_shows_kind_title", "kind", "title"),
    )

    def __repr__(self) -> str:
        return f"<Show(id={self.id}, title={self.title}, kind={self.kind})>"

class Season (Base):

    __tablename__ = "seasons"
    id = Column(Integer, primary_key=True)
    show_id = Column(Integer, ForeignKey("shows.id", ondelete="CASCADE"), nullable=False)
    library_id = Column(Integer, ForeignKey("libraries.id", ondelete="SET NULL"))
    number = Column(Integer, nullable=False)
    path = Column(String, nullable=False)

    show = relationship("Show")
    library = relationship("Library")

    __table_args__ = (
        UniqueConstraint("show_id", "library_id" ,"number", name="uq_season_show_lib_num"),
        CheckConstraint("number >= 0", name="ck_season_number_ge_0"),
        Index("ix_season_show", "show_id"),
        Index("ix_season_lib_num", "library_id", "number"),
    )


    def __repr__(self) ->str:
        return f"<Season(id={self.id}, show_id={self.show_id}, lib_id={self.library_id} ,number={self.number})>"

class Episode (Base):

    __tablename__ = "episodes"

    id = Column(Integer, primary_key=True)
    season_id = Column(Integer, ForeignKey("seasons.id", ondelete="CASCADE"), nullable=False)
    number = Column(Integer)
    file_path = Column(String, nullable=False)
    size = Column(BigInteger)
    hash = Column(String)
    message_link = Column(String)

    season = relationship("Season")

    __table_args__ = (
        UniqueConstraint("season_id", "file_path", name="uq_episode_season_file"),
        Index("ix_episode_season_number", "season_id", "number"),
    )


def __repr__(self) -> str:
        n = f"E{self.number:02d}" if self.number is not None else "E??"
        return f"<Episode id={self.id}, season_id={self.season_id} {n} path={self.file_path}>"

class ChatState(Base):

    __tablename__ = "chat_states"

    chat_id = Column(BigInteger, primary_key=True)
    current_show_id = Column(Integer)
    current_libray_id = Column(Integer)
    current_season = Column(Integer)

    def __repr__(self) -> str:
        return (f"<ChatState(chat_id={self.chat_id}, current_show_id={self.current_show_id}, "
                f"current_library_id={self.current_library_id}, current_season={self.current_season})>")