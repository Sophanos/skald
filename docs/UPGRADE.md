# Upgrade Skald

For the recommended stack, add `-f compose.stack.example.yaml` to each Compose command
and replace `engine` with `skald`. Back up `.env.aiostreams` and the AIOStreams data volume too.

1. Stop the engine with `docker compose stop engine`. Copy the whole DATA folder
   and the `.env` files somewhere private before changing anything.
   This includes credentials and tokens that automatic backups leave out.
   `skald backup list --data /data` is for inspection only; it does not make a complete backup.
2. Run `git pull`, or check out the release tag you chose.
3. Run `docker compose build`.
4. Run `docker compose up -d`.
5. Run `docker compose exec engine skald setup --status` and follow its next command.

Compose builds this checkout with `build: .`; pulling an image alone does not update it.
Skald records its release and data format in `DATA/.skald-format`. Older households
without this file use format 1, today’s layout. Upgrades migrate data automatically
after making and verifying a backup of household state. Credentials and tokens
still need the private copy in step 1. `skald setup --status` shows the release and
data format.

Check the watching app after the upgrade. AIOStreams may need a restart to see changed rows.

If your older household uses `DATA/legacy.json`, run
`docker compose exec engine skald setup adopt --yes` to enable laptop links,
connector setup and imports. Adoption preserves the config and catalog ids.
To undo it, remove `skald.json` (or the path set by `SKALD_CONFIG`) and
`DATA/.skald-household`; Skald loads the unchanged `legacy.json` again.

To roll back, stop the engine, restore the matching DATA backup and `.env`,
check out the previous release, rebuild and start it.
An older release refuses data in a newer format and asks you to upgrade Skald or
restore a backup. Restore the backup made before migration to go back.
Restore into an empty folder first if you need to inspect a backup:
`skald backup restore archive.tar.gz --to /path/empty-folder --dry-run`.
Keep backups private. See [setup](SETUP.md) and [connectors](CONNECTORS.md).
