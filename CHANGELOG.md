# Changelog

Every future release entry includes a **Data changes:** line stating the data
format, migrations, and any backup or rollback steps. Use “none” when the format
and layout stay the same.

## 1.0.2

`skald setup aiostreams` creates an AIOStreams login with Skald and title details
from The Movie Database addon (details only). It prints the Odin / Jellyfin app
login. Existing AIOStreams users can add Skald to their login with `--uuid`;
nothing else in that login changes. Skald's rows stay in Skald's order.

Home only lists rows that have something to show. A row appears once it has
enough titles. Rows no longer share a name in Jellyfin apps.

`.env.aiostreams.example` includes optional `MANIFEST_CACHE_TTL` so new rows
show up within minutes.

**Data changes:** New optional file `DATA/credentials/aiostreams.json`, with
permissions 0600, written only by `skald setup aiostreams`. DATA format unchanged.
No migration.

## 1.0.1

Watch Again no longer repeats a title already shown higher up on the home screen.
The recommended setup now creates a private DATA folder before starting Skald.

**Data changes:** none.

## 1.0.0 — first public release

First public release of Skald: household setup, taste-based shelves, connectors,
and the local CLI.

**Data changes:** Format 1 is the existing DATA layout. Skald records the format
and release in `.skald-format`; households without it count as format 1. Future
upgrades migrate automatically after a verified backup. Older releases refuse
newer data formats; restore the pre-upgrade backup to roll back.

Row ids use the household’s optional `row_prefix`, defaulting to `skald`. Existing
households supply their prefix through `legacy.json`; stored row ids stay intact.

Household setup supports private addon links and a laptop API connection.
History imports preserve member ownership and repeated events. Backups verify
archives before restoring, and offline tools check copied data formats before
loading them. CLI errors explain the next step without exposing credentials.
