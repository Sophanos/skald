"""Fixed SSH commands. Only resolved import events can be uploaded."""
import io
import json
from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile
import os
import re

from .engine import DATA_FILES

REMOTE_DATA = "/opt/skald/data"


def _snapshot_file(name):
    return name in DATA_FILES or bool(re.fullmatch(r"(?:trakt|mdblist|stremio)-[a-z][a-z0-9]{1,15}\.history\.json", name))


def command(operation):
    """The sole command allowlist; no user text is interpolated into a shell."""
    if operation == "snapshot":
        script = ("import os,glob,sys; "
                  "sys.path.insert(0,'/opt/skald/app'); import server; "
                  "blocked=server._backup_credential_inodes(); "
                  "files=[p for p in (glob.glob('trakt-*.history.json')+glob.glob('mdblist-*.history.json')+glob.glob('stremio-*.history.json')) if not os.path.islink(p) and os.path.isfile(p) "
                  "and (os.stat(p).st_dev,os.stat(p).st_ino) not in blocked]; "
                  f"sys.stdout.buffer.write(('\\0'.join({list(DATA_FILES)!r}+files)+'\\0').encode())")
        remote = f"cd {REMOTE_DATA} && DATA_DIR=" + shlex.quote(REMOTE_DATA) + " python3 -c " + shlex.quote(script) + " | tar --ignore-failed-read --null -T - -cf -"
    elif operation == "imports":
        remote = f"if [ -f {REMOTE_DATA}/imports.jsonl ]; then cat {REMOTE_DATA}/imports.jsonl; fi"
    elif operation == "push":
        script = (f"umask 077; cd {REMOTE_DATA} || exit 1; "
                  "if [ -f imports.jsonl ]; then "
                  "cp -p imports.jsonl imports.jsonl.bak-$(date -u +%Y%m%dT%H%M%S)-$$ || exit 1; fi; "
                  "tee imports.jsonl.tmp >/dev/null && chmod 600 imports.jsonl.tmp && mv imports.jsonl.tmp imports.jsonl")
        remote = "sudo -u '#65534' sh -c " + shlex.quote(script)
    elif operation == "push-undo":
        script = ("import datetime,json,os,shutil,sys\n"
                  "sys.path.insert(0,'/opt/skald/app')\n"
                  "from skald.history import locked,remove_imports\n"
                  "from skald.intake import write_private\n"
                  "payload=json.load(sys.stdin)\n"
                  "with locked('.'):\n"
                  "    suffix=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+str(os.getpid())\n"
                  "    for name in ('imports.jsonl','history-summary.json'):\n"
                  "        if os.path.isfile(name): shutil.copy2(name,name+'.bak-'+suffix)\n"
                  "    write_private('imports.jsonl',''.join(json.dumps(e,ensure_ascii=False)+'\\n' for e in payload['imports']))\n"
                  "    remove_imports('.',set(payload['undo_ids']))\n")
        remote = "sudo -u '#65534' sh -c " + shlex.quote(
            f"umask 077; cd {REMOTE_DATA} || exit 1; python3 -c " + shlex.quote(script))
    else:
        raise ValueError("operation is not allowlisted")
    host = os.environ.get("SKALD_SSH_HOST", "")
    if not host or not re.fullmatch(r"[A-Za-z0-9_.@-]+", host) or host.startswith("-"):
        raise ValueError("set SKALD_SSH_HOST to your SSH host, or use --remote for the API")
    return ["ssh", "-o", "BatchMode=yes", host, remote]


def pull(destination=None, run=None):
    run = run or subprocess.run
    destination = Path(destination or Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "skald/live")
    result = run(command("snapshot"), check=True, capture_output=True, timeout=60)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".skald-live-", dir=destination.parent) as temp:
        work = Path(temp)
        seen = set()
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:*") as archive:
            for member in archive:
                if not _snapshot_file(member.name) or not member.isfile() or member.name in seen or member.size > 100_000_000:
                    raise ValueError("snapshot archive contains an unapproved entry")
                seen.add(member.name)
                with archive.extractfile(member) as src, open(work / member.name, "wb") as out:
                    os.fchmod(out.fileno(), 0o600)
                    out.write(src.read())
        if not {"emb.json", "facets.json"} <= seen or not (seen & {"profile.json", "history.json", "trakt.json"}
                or any(name.endswith(".history.json") for name in seen)):
            raise ValueError("live snapshot is missing required engine files")
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in {*DATA_FILES, *seen, *(p.name for p in destination.iterdir() if _snapshot_file(p.name))}:
            if name in seen:
                os.replace(work / name, destination / name)
            else:
                (destination / name).unlink(missing_ok=True)
    return destination


def push(events, validate, apply=False, run=None, undone=()):
    local = [validate(e) for e in events if e.get("id") not in undone]
    upload_command = command("push-undo" if undone else "push")
    preview = {"apply": apply, "events": local, "count": len(local), "undo_ids": sorted(undone), "host": upload_command[3],
               "command": upload_command, "backup": "imports.jsonl.bak-UTC-pid"}
    if not apply:
        return preview
    run = run or subprocess.run
    result = run(command("imports"), check=True, capture_output=True, timeout=60)
    remote = [validate(json.loads(line)) for line in result.stdout.decode("utf-8").splitlines()]
    if any(not {"t", "id"} <= set(e) for e in remote + local):
        raise ValueError("push requires stored events with ids and day dates")
    merged = {e["id"]: e for e in remote if e["id"] not in undone}
    for e in local:
        if e["id"] in merged and merged[e["id"]] != e:
            raise ValueError("conflicting import id")
        merged[e["id"]] = e
    if len(merged) > 20000:
        raise ValueError("remote imports would exceed 20000 events")
    raw = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in merged.values()).encode("utf-8")
    if undone:
        raw = json.dumps({"imports": list(merged.values()), "undo_ids": sorted(undone)}, ensure_ascii=False).encode("utf-8")
    run(upload_command, input=raw, check=True, capture_output=True, timeout=60)
    return {**preview, "uploaded": len(merged)}
