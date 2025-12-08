# config/settings.py
import os
import yaml
from dataclasses import dataclass
from typing import List, Literal, Optional

LibraryType = Literal["movie", "series", "anime", "documentary", "docuseries"]

@dataclass
class LibraryCfg:
    name: str
    type: LibraryType
    root: str

@dataclass
class DownloadCfg:
    # Template uses double-escaped braces so the final command gets {{ .FileName }} (keeps original filename with extension).
    tdl_template: str = 'tdl dl -u {url} -d "{dir}" -t 16 -l 9 --reconnect-timeout 0 --template "{{{{ .FileName }}}}"'
    tdl_home: str = os.getenv("TDL_HOME", os.path.expanduser("~/.tdl-plexbot"))
    extract_rar: bool = True

@dataclass
class Settings:
    db_url: str
    libraries: List[LibraryCfg]
    download: DownloadCfg
    admin_chat_id: Optional[str]
    telegram_token: Optional[str]

def load_settings(yaml_path: str = "config/libraries.yaml") -> Settings:
    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    libs = [
        LibraryCfg(name=str(i["name"]), type=str(i["type"]), root=str(i["root"]))
        for i in data.get("libraries", [])
    ]

    dl = data.get("download", {}) or {}
    download = DownloadCfg(
        tdl_template=dl.get("tdl_template", DownloadCfg.tdl_template),
        tdl_home=dl.get("tdl_home", DownloadCfg.tdl_home),
        extract_rar=bool(dl.get("extract_rar", True)),
    )

    db_url = os.getenv("PLEX_DB_URL", "sqlite:///plexbot.db")

    admin_chat_id_env = (data.get("admin", {}) or {}).get("chat_id_env", "ADMIN_CHAT_ID")
    telegram_token_env = (data.get("telegram", {}) or {}).get("token_env", "TELEGRAM_BOT_TOKEN")
    admin_chat_id = os.getenv(admin_chat_id_env)
    telegram_token = os.getenv(telegram_token_env)

    return Settings(
        db_url=db_url,
        libraries=libs,
        download=download,
        admin_chat_id=admin_chat_id,
        telegram_token=telegram_token,
    )
