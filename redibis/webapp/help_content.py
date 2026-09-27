"""Help page content (static/help.json) shared by /help and the static docs site.

No FastAPI import, so the GitHub Pages build script can use it too.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("redibis.webapp")

HELP_JSON = Path(__file__).resolve().parent / "static" / "help.json"


def load_help_content(path: Path = HELP_JSON) -> dict:
    """Load help sections; only http(s) video links survive (no javascript: etc.)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        logger.warning("help.json missing or invalid", exc_info=True)
        return {"intro": "", "sections": []}
    for sec in data.get("sections") or []:
        for vid in sec.get("videos") or []:
            url = str(vid.get("url") or "").strip()
            vid["url"] = url if url.lower().startswith(("https://", "http://")) else ""
    return data
