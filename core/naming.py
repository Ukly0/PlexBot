import re
from typing import Optional

def safe_title(name: str, year: Optional[int] = None) -> str:
    """Sanitize a title for filesystem usage and optionally append year."""
    cleaned = re.sub(r"[\\/:*?\"<>|]", " ", name).strip()
    return f"{cleaned} ({year})" if year else cleaned
