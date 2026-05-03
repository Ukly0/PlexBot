"""Load .env file and libraries.yaml into a Settings dataclass."""

import os
import yaml
from dataclasses import dataclass, field
from typing import Optional


def load_env_file(path: str = ".env") -> None:
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def _parse_id_set(value: Optional[str]) -> set[str]:
    if not value:
        return set()
    normalized = value.replace(",", " ")
    return {part.strip() for part in normalized.split() if part.strip()}


@dataclass
class Library:
    name: str
    type: str  # "series" or "movie"
    root: str


@dataclass
class DownloadCfg:
    tdl_template: str = (
        'tdl dl -u {url} -d "{dir}" -t 16 -l 9 --reconnect-timeout 0 '
        '--template "{{ .FileName }}"'
    )
    tdl_home: str = ""


@dataclass
class Settings:
    libraries: list[Library] = field(default_factory=list)
    download: DownloadCfg = field(default_factory=DownloadCfg)
    admin_chat_id: Optional[str] = None
    admin_user_ids: set[str] = field(default_factory=set)
    allowed_chat_ids: set[str] = field(default_factory=set)
    telegram_token: Optional[str] = None


def load_settings(yaml_path: str = "config/libraries.yaml") -> Settings:
    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    libs = [
        Library(
            name=str(lib["name"]),
            type=str(lib["type"]).strip().lower(),
            root=str(lib["root"]),
        )
        for lib in data.get("libraries", [])
    ]

    dl = data.get("download", {}) or {}
    download = DownloadCfg(
        tdl_template=dl.get("tdl_template", DownloadCfg.tdl_template),
        tdl_home=dl.get("tdl_home", DownloadCfg.tdl_home),
    )

    admin_user_ids = _parse_id_set(os.getenv("ADMIN_USER_IDS"))
    legacy_admin = os.getenv("ADMIN_CHAT_ID")
    if legacy_admin:
        admin_user_ids.update(_parse_id_set(legacy_admin))

    settings = Settings(
        libraries=libs,
        download=download,
        admin_chat_id=os.getenv("ADMIN_CHAT_ID"),
        admin_user_ids=admin_user_ids,
        allowed_chat_ids=_parse_id_set(os.getenv("ALLOWED_CHAT_IDS")),
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN"),
    )
    return settings
