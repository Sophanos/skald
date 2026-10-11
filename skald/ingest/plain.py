import re
from . import Candidate, clean_title


SOURCE = {"hint": "Title list", "dir": False, "profiles": False}

def parse(path, **kwargs):
    out = []
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            if not line.strip():
                continue
            match = re.fullmatch(r"\s*(.*?)\s*\((\d{4})\)\s*", line.strip())
            out.append(Candidate(clean_title(match[1] if match else line), match[2] if match else ""))
    return out
