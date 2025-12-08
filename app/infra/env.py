import os
from pathlib import Path


def load_env_file(path: str = ".env") -> None:
    """
    Load simple KEY=VALUE pairs from a .env file if it exists.
    Lines starting with # are ignored. No interpolation.
    """
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip())
