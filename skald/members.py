"""Load only the server's stdlib config section; no server threads or API keys."""
import copy
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_path = Path(__file__).resolve().parents[1] / "server.py"
_source = _path.read_text().split("# --- household config (also loaded alone by skald/members.py) ---", 1)[1].split("# --- end household config ---", 1)[0]
exec(compile(_source, str(_path), "exec"))
PEOPLE = []
CONFIG_ERROR = None


def configure(data=None, *, readonly=False):
    global CONFIG, CONFIG_SOURCE, CONFIG_ERROR
    if isinstance(data, str) and not data.strip():
        data = None
    from .migrate import check, FormatError
    CONFIG_ERROR = None
    try:
        check(data or os.environ.get("DATA_DIR", "/data"))
        env = {k: v for k, v in os.environ.items() if not k.startswith("SKALD_")} if data is not None else os.environ
        CONFIG, CONFIG_SOURCE = load_config(data, env, readonly=readonly or data is not None)
    except (ConfigError, FormatError) as e:
        CONFIG, CONFIG_SOURCE, CONFIG_ERROR = copy.deepcopy(LEGACY), "invalid", e
    PEOPLE[:] = CONFIG["household"]["members"]
    return CONFIG


configure(readonly=True)
