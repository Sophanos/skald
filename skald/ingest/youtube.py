import datetime as dt
from html.parser import HTMLParser
import re
from . import Candidate, clean_title, day, interests, iter_json_array


SOURCE = {"hint": "Takeout watch history (JSON or HTML)", "dir": False, "profiles": False}


def html_day(text):
    text = text.rsplit(",", 1)[0].strip().replace("Sept", "Sep")
    for fmt in ("%d %b %Y", "%b %d, %Y", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


class WatchHistory(HTMLParser):
    def __init__(self, add):
        super().__init__()
        self.add = add
        self.parts = None
        self.link = False

    def handle_starttag(self, tag, attrs):
        if tag == "div" and set(dict(attrs).get("class", "").split()) == {
                "content-cell", "mdl-cell", "mdl-cell--6-col", "mdl-typography--body-1"}:
            self.parts = []
            self.link = False
        elif self.parts is not None:
            if tag == "br":
                self.parts.append("\n")
            elif tag == "a":
                self.link = True

    def handle_data(self, data):
        if self.parts is not None:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "div" and self.parts is not None:
            text = "".join(self.parts).translate(str.maketrans("\u00a0\u202f\u2009", "   "))
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if self.link and lines and re.match(r"^Watched\s", lines[0]):
                self.add({"title": lines[0], "time": html_day(lines[-1])})
            self.parts = None

def parse(path, **kwargs):
    out = []
    newest = ""

    def add(row):
        nonlocal newest
        stamp = day(row.get("time"))
        newest = max(newest, stamp)
        text = re.sub(r"^Watched\s+", "", row.get("title", ""), flags=re.I)
        match = re.search(r"\b(?:trailer|teaser|official\s+clip)\b", text, re.I)
        if not match:
            return
        title = re.sub(r"\b(?:official|final|new|full)\s*$", "", text[:match.start()], flags=re.I)
        title = title.strip(" -–—|:")
        year_match = re.search(r"\((\d{4})\)", text)
        year = year_match[1] if year_match else ""
        title = re.sub(r"\(\d{4}\)", "", title).strip()
        if title:
            out.append(Candidate(clean_title(title), year, "", {}, "interest", 1, stamp, "youtube", conf=.5))

    with open(path, encoding="utf-8") as fh:
        content = fh.read(65536)
        while content and not content.lstrip():
            chunk = fh.read(65536)
            if not chunk:
                break
            content += chunk
        if content.lstrip().startswith("<"):
            history = WatchHistory(add)
            history.feed(content)
            for chunk in iter(lambda: fh.read(65536), ""):
                history.feed(chunk)
        else:
            for row in iter_json_array(fh, content):
                add(row)
    return interests(out, newest)
