"""Local state archives. A live restore is an owner-run, stopped-container step."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tarfile
import zlib
from skald import members as config_module

CONNECTOR_BACKUP = os.environ.get("SKALD_CONNECTOR_BACKUP", "1") != "0"
BACKUP_FILES = ("commands.json", "skald.json", "legacy.json", ".skald-household", ".skald-format", "feedback.jsonl", "imports.jsonl", "sessions.jsonl", "taste.json", "trakt.json",
                "history.json", "history-summary.json", "history-forgotten.json", "health-settings.json", "learn.json", "online.json", "metrics.json", "lab.json", "decisions.jsonl", "usage.json",
                "searches.jsonl", "emb.json", "facets.json", "memory.json", "memory.log.jsonl", "memory.prev.json", "interview.json", "shelves.json", "undo.jsonl", "wishes.jsonl", "shelf_rules.json",
                "title_fixes.json", "archetypes.json") + (("connectors.json",) if CONNECTOR_BACKUP else ())
_NAME = re.compile(r"state-(\d{4}-\d{2}-\d{2})\.tar\.gz")
_MIGRATION_NAME = re.compile(r"pre-migrate-\d+-\d+-(\d+)-[0-9a-f]{32}\.tar\.gz")
_MB = 1024 * 1024


def before_people(s, cfg):
    """Keep a private config copy before a people change; evidence stays in place."""
    from .intake import write_private
    import uuid
    root = Path(s.DATA) / 'backups'
    root.mkdir(mode=0o700, exist_ok=True)
    path = root / ('pre-people-' + uuid.uuid4().hex + '.json')
    write_private(path, json.dumps(cfg, ensure_ascii=False) + '\n')
    return path


def retain(days):
    days = sorted(set(days), reverse=True)
    keep, weeks = set(days[:14]), {}
    for day in days[14:]:
        week = datetime.date.fromisoformat(day).isocalendar()[:2]
        if week in weeks or len(weeks) < 8:
            weeks[week] = day
    return keep | set(weeks.values())


def migration_archives(root):
    rows = []
    for path in Path(root).iterdir():
        match = _MIGRATION_NAME.fullmatch(path.name)
        if match and path.is_file() and not path.is_symlink():
            rows.append((int(match[1]), path.name))
    return [name for _, name in sorted(rows, reverse=True)]


def list_backups(data):
    root = Path(data)
    if (root / "backups").is_dir():
        root /= "backups"
    rows = []
    for path in root.iterdir():
        match = _NAME.fullmatch(path.name)
        if match:
            try:
                datetime.date.fromisoformat(match[1])
            except ValueError:
                continue
            if path.is_file() and not path.is_symlink():
                rows.append({"name": path.name, "day": match[1], "bytes": path.stat().st_size})
    rows.sort(key=lambda row: row["day"], reverse=True)
    keep = retain(row["day"] for row in rows)
    for i, row in enumerate(rows):
        row["keep"] = "daily" if i < 14 else "weekly" if row["day"] in keep else "prune"
    for i, name in enumerate(migration_archives(root)):
        rows.append({"name": name, "bytes": (root/name).stat().st_size,
                     "kind": "pre-migration", "keep": "pre-migration" if i < 3 else "prune"})
    return {"archives": rows}


def _hash(src):
    sha, size = hashlib.sha256(), 0
    while raw := src.read(65536):
        size += len(raw)
        sha.update(raw)
    return sha.hexdigest(), size


def _member(member, seen, total):
    name = member.name
    if member.type != tarfile.REGTYPE or not name or "/" in name or ".." in name or not (
            name in (*BACKUP_FILES, "manifest.json") or re.fullmatch(r"(?:trakt|mdblist|stremio|pmdb)-[a-z][a-z0-9]{1,15}\.history\.json", name)):
        raise ValueError("unapproved archive member")
    if name in seen:
        raise ValueError("duplicate archive member")
    if member.size < 0 or member.size > 64 * _MB or total + member.size > 512 * _MB:
        raise ValueError("archive members too large")


def _manifest(value, files):
    if not isinstance(value, dict) or type(value.get("v")) is not int or value["v"] != 1 \
            or type(value.get("created")) is not int or not isinstance(value.get("day"), str):
        raise ValueError("invalid backup manifest")
    try:
        if datetime.date.fromisoformat(value["day"]).isoformat() != value["day"]:
            raise ValueError
    except ValueError:
        raise ValueError("invalid backup day") from None
    records, skipped = value.get("files"), value.get("skipped")
    if not isinstance(records, dict) or set(records) != set(files) or not isinstance(skipped, dict) \
            or any(not (k in BACKUP_FILES or re.fullmatch(r"(?:trakt|mdblist|stremio|pmdb)-[a-z][a-z0-9]{1,15}\.history\.json", k)) or not isinstance(v, str) for k, v in skipped.items()):
        raise ValueError("manifest members mismatch")
    for name, rec in records.items():
        if not isinstance(rec, dict) or type(rec.get("size")) is not int or type(rec.get("mtime")) is not int \
                or not isinstance(rec.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", rec["sha256"]):
            raise ValueError("invalid backup file receipt")
        if rec["size"] != files[name]["size"] or rec["sha256"] != files[name]["sha256"]:
            raise ValueError("backup hash or size mismatch")
    if value.get("household") or set(files) & {"skald.json", ".skald-household"}:
        if not {"skald.json", ".skald-household"} <= set(files):
            raise ValueError("household archive needs skald.json and .skald-household")
    return value


def _verified(path):
    report = {"ok": False, "day": None, "files": {}, "problems": []}
    files, seen, total, manifest = {}, set(), 0, None
    with open(path, "rb") as fh:
        if os.fstat(fh.fileno()).st_size > 256 * _MB:
            report["problems"].append("archive too large")
            return report, None
        try:
            with tarfile.open(fileobj=fh, mode="r|gz") as archive:
                for member in archive:
                    _member(member, seen, total)
                    if manifest is not None:
                        raise ValueError("manifest must be last")
                    seen.add(member.name)
                    total += member.size
                    with archive.extractfile(member) as src:
                        if member.name == "manifest.json":
                            if member.size > 65536:
                                raise ValueError("manifest too large")
                            manifest = json.loads(src.read())
                        else:
                            if member.name in ("skald.json", "legacy.json"):
                                raw = src.read()
                                config_module.validate_config(json.loads(raw, parse_constant=config_module.invalid_json), environ={})
                                sha, size = hashlib.sha256(raw).hexdigest(), len(raw)
                            else:
                                sha, size = _hash(src)
                            files[member.name] = {"size": size, "sha256": sha}
                if manifest is None:
                    raise ValueError("backup manifest missing")
                manifest = _manifest(manifest, files)
            report.update(ok=True, day=manifest["day"])
        except (tarfile.TarError, json.JSONDecodeError, UnicodeError, EOFError, RecursionError, zlib.error) as e:
            raise ValueError("not a valid backup archive") from e
        except ValueError as e:
            report["problems"].append(str(e))
    records = manifest.get("files", {}) if isinstance(manifest, dict) else {}
    for name, rec in files.items():
        receipt = records.get(name) if isinstance(records, dict) else None
        report["files"][name] = {"size": rec["size"], "ok": isinstance(receipt, dict)
                                 and all(receipt.get(k) == v for k, v in rec.items())}
    return report, manifest


def verify(path):
    return _verified(path)[0]


def _destination(to, snapshot):
    target = Path(to)
    if target.is_symlink():
        raise ValueError("restore destination is a link")
    resolved = target.resolve()
    env_snapshot = os.environ.get("SKALD_SNAPSHOT")
    env_snapshot = env_snapshot if env_snapshot and env_snapshot.strip() else None
    for root in ("/data", os.environ.get("DATA_DIR"), snapshot, env_snapshot):
        if root and resolved.is_relative_to(Path(root).resolve()):
            raise ValueError("restore destination must be outside live data and snapshots")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError("restore destination must be empty")
    return target


def restore(path, to, dry_run=False, snapshot=None):
    target = _destination(to, snapshot)
    report, manifest = _verified(path)
    if not report["ok"]:
        return report
    plan = {"ok": True, "day": report["day"], "files": report["files"], "dry_run": dry_run}
    if dry_run:
        return plan
    target = _destination(to, snapshot)
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    try:
        with tarfile.open(path, "r:gz") as archive:
            members, seen, total = [], set(), 0
            for member in archive:
                _member(member, seen, total)
                seen.add(member.name)
                total += member.size
                if member.name == "manifest.json":
                    if member.size > 65536 or json.loads(archive.extractfile(member).read()) != manifest or archive.next() is not None:
                        raise ValueError("archive changed during restore")
                    break
                rec = manifest["files"].get(member.name)
                if not rec or rec["size"] != member.size:
                    raise ValueError("archive changed during restore")
                members.append(member)
            if seen != set(manifest["files"]) | {"manifest.json"}:
                raise ValueError("archive changed during restore")
            config = [m for m in members if m.name in ("skald.json", "legacy.json", ".skald-household")]
            if config:
                archive.extractall(target, members=config, filter="data")
                for member in config:
                    file = target / member.name
                    os.chmod(file, 0o600)
                    with file.open("rb") as src:
                        sha, size = _hash(src)
                        os.fsync(src.fileno())
                    rec = manifest["files"][member.name]
                    if sha != rec["sha256"] or size != rec["size"]:
                        raise ValueError("restored file hash or size mismatch")
                if "skald.json" in manifest["files"]:
                    config_module.load_config(str(target), {}, readonly=True)
                    config_module.stamp_household(str(target))
            archive.extractall(target, members=[m for m in members if m not in config], filter="data")
        for name, rec in manifest["files"].items():
            file = target / name
            os.chmod(file, 0o600)
            with file.open("rb") as src:
                sha, size = _hash(src)
            if sha != rec["sha256"] or size != rec["size"]:
                raise ValueError("restored file hash or size mismatch")
    except (tarfile.TarError, json.JSONDecodeError, UnicodeError, EOFError, RecursionError, zlib.error) as e:
        raise ValueError("not a valid backup archive") from e
    return plan


def credential_inodes(data):
    def unreadable(error):
        raise error
    try:
        directory = os.open(os.path.join(data, "credentials"), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return set()
    try:
        inodes = set()
        for _, _, names, fd in os.fwalk(".", dir_fd=directory, follow_symlinks=False, onerror=unreadable):
            for name in names:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISREG(st.st_mode):
                    inodes.add((st.st_dev, st.st_ino))
        return inodes
    finally:
        os.close(directory)


def create(data, *, from_format=1, to_format=1):
    """Reuse the server's streamed, verified backup before any migration."""
    import time
    import uuid
    from .setup import server
    with server(data) as s:
        now = time.time()
        name = f'pre-migrate-{from_format}-{to_format}-{time.time_ns()}-{uuid.uuid4().hex}.tar.gz'
        s.backup_state(now, archive_name=name)
        path = Path(data)/'backups'/name
    if not verify(path)['ok']:
        raise ValueError('migration backup verification failed')
    return path


def run(args):
    if args.backup == "list":
        return list_backups(args.data), True
    if args.backup == "verify":
        data = verify(args.archive)
    else:
        data = restore(args.archive, args.to, args.dry_run, getattr(args, "snapshot", None))
    return data, data["ok"]


def text(data):
    if "archives" in data:
        return [f"{r['name']}  {r['bytes']} bytes  {r['keep']}"
                + (" (pre-migration)" if r.get("kind") == "pre-migration" else "") for r in data["archives"]]
    return [f"{'ok' if data['ok'] else 'failed'} · {data.get('day') or 'unknown day'} · {len(data['files'])} files"
            + (" · dry run" if data.get("dry_run") else ""), *data.get("problems", [])]
