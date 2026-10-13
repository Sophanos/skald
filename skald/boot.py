"""Keep an empty container available for docker compose exec engine skald setup."""
import os
from pathlib import Path
import sys

from .members import load_config, ConfigError


def main(out=None):
    out = out or sys.stdout
    from .migrate import check, ensure, FORMAT, FormatError
    data = os.environ.get('DATA_DIR', '/data')
    try:
        if check(data)['format'] < FORMAT:
            ensure(data)
    except (FormatError, OSError) as e:
        print(str(e), file=sys.stderr)
        return 2
    try:
        _, source = load_config(readonly=True)
    except ConfigError:
        source = 'invalid'  # Let the server report an invalid existing household.
    if source == 'implicit':
        command = 'docker compose -f compose.stack.example.yaml exec skald skald setup' if os.environ.get('SKALD_INTERNAL_BASE') else 'docker compose exec engine skald setup'
        print('Empty DATA. Run: ' + command, file=out, flush=True)
    os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve().parents[1]/'server.py')])


if __name__ == '__main__':
    raise SystemExit(main())
