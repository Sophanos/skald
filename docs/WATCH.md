# Where you watch

## Recommended: the setup we run

Tested by us. OpenRouter adds natural-language AI search to this setup.
Only AIOStreams needs an HTTPS address. Skald stays inside the Docker network.

```text
Apple TV app (Odin) → AIOStreams (Jellyfin mode, HTTPS) → Skald (internal)
```

1. Copy `.env.example` to `.env` and `.env.aiostreams.example` to `.env.aiostreams`.
   Set TMDB and OpenRouter keys in `.env`. Make the AIOStreams secret with
   `openssl rand -hex 32`; set its public HTTPS address in `.env.aiostreams`.
2. Run `docker compose -f compose.stack.example.yaml up -d --build`.
3. Run `docker compose -f compose.stack.example.yaml exec skald skald setup`.
4. Add the printed internal manifest URL as a custom addon in AIOStreams.
5. Add your AIOStreams HTTPS address as a server in Odin. Log in with the AIOStreams
   config UUID as username and the config password.

Stremio can use the same AIOStreams URL; that path should work, not yet tested by us.
AIOStreams is a separate project with its own [docs and licence](https://github.com/Viren070/AIOStreams).
We reference its published image. Configure its stream addons for playback.

Stremio direct — should work, not yet tested by us.
Open the printed member URL (or Together) in Stremio → Install.
Skald only adds rows and search; keep your stream addons for playback.

AIOStreams in Stremio, self-hosted or ElfHosted — should work, not yet tested by us.
Add Skald as a custom addon by its manifest URL: YOUR_PRINTED_MANIFEST_URL
On the same Docker network, use http://skald:8080/<addon-token>/<lang>/manifest.json;
otherwise use the printed HTTPS URL. The internal URL includes the private addon token.
copy the exact URL that `skald setup --watch aiostreams` prints.
Order rows in AIOStreams' catalog settings.
AIOStreams caches addon manifests for hours: restart AIOStreams (or wait)
after Skald adds or renames rows.

Apple TV / Jellyfin apps such as Odin — tested by us.
First add Skald to AIOStreams as a custom addon: http://skald:8080/<addon-token>/<lang>/manifest.json
Use the printed HTTPS URL if AIOStreams is outside this Docker network.
AIOStreams can pretend to be a Jellyfin server. Enable JELLYFIN_ENABLED.
Only the first JELLYFIN_MAX_LIBRARIES catalogs become libraries (default 20).
Raise it if you have more rows; we use 40.
In the app, add a server at your AIOStreams address. Log in with the AIOStreams
config UUID as username and the config password. Skald rows appear as libraries on Home.
Rows mixing films and series are served as type "tv" so series show up.
To learn from viewing there, run skald setup --connect aiostreams-db.
A shared login can count as "together".

Plex playback should work, not yet tested by us. Skald does not supply streams.
