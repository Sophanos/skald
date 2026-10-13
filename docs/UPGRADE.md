# Upgrade Skald

For the recommended stack, add `-f compose.stack.example.yaml` to each Compose command
and replace `engine` with `skald`. Back up `.env.aiostreams` and the AIOStreams data volume too.

1. Stop the engine with `docker compose stop engine`. Copy the whole DATA folder
   and the `.env` files somewhere private before changing anything.
   This includes credentials and tokens that automatic backups leave out.
   Back up any external `SKALD_CONFIG` file separately.
   `skald backup list --data /data` is for inspection only; it does not make a complete backup.
2. Run `git pull`, or check out the release tag you chose.
3. Run `docker compose build`.
4. Run `docker compose up -d`.
5. Run `docker compose exec engine skald setup --status` and follow its next command.

Compose builds this checkout with `build: .`; pulling an image alone does not update it.
Skald records its release and data format in `DATA/.skald-format`. Older households
without this file use format 1. Format 2 declares optional household settings.
Format 3 adds local weekly row metrics when enabled; metrics default off and start
empty because old row pins have no delivery times.
Format 4 adds settings history metadata while keeping undated old entries.
Format 5 adds an empty summary store for optional history minimisation, which
defaults off and leaves existing raw history intact.
Read-only commands leave these
additive migrations to engine startup. Upgrades migrate data automatically
after making and verifying a backup of household state. Credentials and tokens
still need the private copy in step 1. `skald setup --status` shows the release and
data format.

Check the watching app after the upgrade. AIOStreams may need a restart to see changed rows.

If your older household uses `DATA/legacy.json`, run
`docker compose exec engine skald setup adopt --yes` to enable laptop links,
connector setup and imports. Adoption preserves the config and catalog ids.
To undo it, remove `skald.json` (or the path set by `SKALD_CONFIG`) and
`DATA/.skald-household`; Skald loads the unchanged `legacy.json` again.

To roll back, keep the engine stopped. Restore the matching pre-upgrade backup into
an empty folder, then replace the DATA folder with it, preserving the original
owner and permissions. Do not overlay a tar archive onto DATA: newer files would remain.
Restore `.env` and any external `SKALD_CONFIG` from your private copies.

Restore an automatic pre-migration archive with the new release's `skald backup restore`
before switching back, because the older verifier rejects its manifest:
`skald backup restore archive.tar.gz --to /path/empty-folder`.
Add credentials and tokens from your private copy to the restored folder before replacing DATA.
Then check out the previous release, rebuild and start it. An older release refuses
data in a newer format; use the backup made before migration.
Keep backups private. See [setup](SETUP.md) and [connectors](CONNECTORS.md).
