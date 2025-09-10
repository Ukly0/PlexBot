# cli/manage.py
"""
CLI de mantenimiento del proyecto.
Comandos:
  - init-db                      Crea tablas en la BD
  - seed-libs                    Inserta bibliotecas desde config/libraries.yaml (idempotente)
  - libs                         Lista bibliotecas registradas
  - scan [--all|--type T|--library NOMBRE]  Escanea el FS y vuelca Show/Season/Episode
  - stats                        Muestra métricas rápidas

Uso recomendado (desde la raíz del proyecto):
    python -m cli.manage init-db
    python -m cli.manage seed-libs
    python -m cli.manage libs
    python -m cli.manage scan --all
    python -m cli.manage stats
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from config.settings import load_settings
from store.base import make_engine, make_session_factory, Base
from store.models import Library, LibraryType, Show, Season, Episode
from store.repos import LibraryRepo
from fs.scanner import scan_all_libraries, scan_libraries_by_type, scan_library

from sqlalchemy import select, or_, func
from sqlalchemy.orm import Session

# ----------------------------
# Infra
# ----------------------------

def make_session() -> tuple[Session, object]:
    st = load_settings()
    engine = make_engine(st.db_url)
    SessionLocal = make_session_factory(engine)
    return SessionLocal(), st


# ----------------------------
# Comandos
# ----------------------------

def cmd_init_db(args):
    st = load_settings()
    engine = make_engine(st.db_url)
    Base.metadata.create_all(bind=engine)
    print("✔ Tablas creadas (o ya existían). DB:", st.db_url)


def cmd_seed_libs(args):
    s, st = make_session()
    try:
        repo = LibraryRepo(s)
        inserted = 0
        for cfg in st.libraries:
            # idempotente por name o root
            exists = s.execute(
                select(Library).where(or_(Library.name == cfg.name, Library.root == cfg.root))
            ).scalar_one_or_none()
            if exists:
                continue
            repo.upsert_from_cfg(cfg.name, LibraryType(cfg.type), cfg.root)
            inserted += 1
        if inserted:
            s.commit()
        print(f"✔ Librerías nuevas insertadas: {inserted}")
        # Mostrar
        libs = repo.list_all()
        print("Librerías registradas:")
        for l in libs:
            print(f" - {l.id}: {l.name} [{l.type.value}] -> {l.root}")
    finally:
        s.close()


def cmd_libs(args):
    s, _ = make_session()
    try:
        repo = LibraryRepo(s)
        libs = repo.list_all()
        if not libs:
            print("No hay bibliotecas registradas. Ejecuta: python -m cli.manage seed-libs")
            return
        print("Bibliotecas:")
        for l in libs:
            print(f"{l.id:>3}  {l.name:<16}  {l.type.value:<12}  {l.root}")
    finally:
        s.close()


def cmd_scan(args):
    s, _ = make_session()
    try:
        verbose = bool(args.verbose)
        if args.all or (not args.type and not args.library):
            stats = scan_all_libraries(s, verbose=verbose)
            print(f"✔ Scan ALL → +{stats.shows_new} shows, +{stats.seasons_new} seasons, +{stats.episodes_new} episodes (files seen: {stats.files_seen})")
            return

        if args.library:
            lib = LibraryRepo(s).by_name(args.library)
            if not lib:
                print(f"✘ No existe la biblioteca con name='{args.library}'. Usa 'libs' para listar.")
                return
            stats = scan_library(s, lib, verbose=verbose)
            print(f"✔ Scan LIB:{lib.name} → +{stats.shows_new} shows, +{stats.seasons_new} seasons, +{stats.episodes_new} episodes (files seen: {stats.files_seen})")
            return

        if args.type:
            try:
                lib_type = LibraryType(args.type)
            except ValueError:
                print("✘ Tipo inválido. Usa: series | anime | docuseries | documentary | movie")
                return
            stats = scan_libraries_by_type(s, lib_type, verbose=verbose)
            print(f"✔ Scan TYPE:{lib_type.value} → +{stats.shows_new} shows, +{stats.seasons_new} seasons, +{stats.episodes_new} episodes (files seen: {stats.files_seen})")
            return
    finally:
        s.close()


def cmd_stats(args):
    s, _ = make_session()
    try:
        # Shows por tipo
        rows = s.execute(
            select(Show.kind, func.count()).group_by(Show.kind).order_by(Show.kind)
        ).all()
        print("Shows por tipo:")
        if not rows:
            print("  (vacío)")
        else:
            for kind, count in rows:
                print(f"  - {kind.value:<12} {count}")

        # Totales
        tot_seasons = s.scalar(select(func.count()).select_from(Season)) or 0
        tot_episodes = s.scalar(select(func.count()).select_from(Episode)) or 0
        print(f"Seasons totales:  {tot_seasons}")
        print(f"Episodes totales: {tot_episodes}")

        # Top 10 shows (por nº de temporadas)
        top = s.execute(
            select(Show.title, func.count(Season.id).label("n"))
            .join(Season, Season.show_id == Show.id, isouter=True)
            .group_by(Show.id)
            .order_by(func.count(Season.id).desc(), Show.title.asc())
            .limit(10)
        ).all()
        if top:
            print("Top 10 por nº de temporadas:")
            for title, n in top:
                print(f"  - {title} ({n or 0})")
    finally:
        s.close()


# ----------------------------
# Parser
# ----------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cinebot-manage", description="CLI de mantenimiento")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init-db", help="Crea tablas en la BD")
    sp.set_defaults(func=cmd_init_db)

    sp = sub.add_parser("seed-libs", help="Inserta bibliotecas desde config/libraries.yaml (idempotente)")
    sp.set_defaults(func=cmd_seed_libs)

    sp = sub.add_parser("libs", help="Lista bibliotecas registradas")
    sp.set_defaults(func=cmd_libs)

    sp = sub.add_parser("scan", help="Escanea el FS y vuelca Show/Season/Episode")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="Escanear TODAS las bibliotecas (por defecto si no pasas nada)")
    g.add_argument("--type", choices=[t.value for t in LibraryType], help="Escanear por tipo (series, anime, docuseries, documentary, movie)")
    g.add_argument("--library", help="Escanear una biblioteca por nombre (p. ej. SeriesDisk1)")
    sp.add_argument("-v", "--verbose", action="store_true", help="Más salida de log")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("stats", help="Muestra métricas rápidas (shows/seasons/episodes)")
    sp.set_defaults(func=cmd_stats)

    return p


def main(argv: Optional[list[str]] = None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
