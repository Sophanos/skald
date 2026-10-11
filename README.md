# Skald

Skald makes personal rows and search for your household, with a shared Together view.
It learns from watch history and ratings. Your existing streaming addons still play things.

## What you need

- A free [TMDB API key](https://www.themoviedb.org/settings/api).
- A machine with Docker and Docker Compose.
- An HTTPS address if watching apps outside your network must reach the stack.
- Optional: an OpenRouter key for AI picks, natural-language search, mood shelves and weekly picks.
  Rows and title search work without it.

## Quickstart

Run on the data machine:

```sh
git clone https://github.com/Sophanos/skald skald && cd skald
mkdir -p data && sudo chown 65534 data && sudo chmod 700 data
cp .env.example .env && cp .env.aiostreams.example .env.aiostreams && chmod 600 .env .env.aiostreams
${EDITOR:-vi} .env .env.aiostreams
docker compose -f compose.stack.example.yaml up -d --build
docker compose -f compose.stack.example.yaml exec skald skald setup
```

Put `TMDB_API=YOUR_KEY` and `OPENROUTER_API_KEY=YOUR_KEY` in `.env`.
Delete the `SKALD_MEMBERS`, `SKALD_LANG` and `SKALD_TZ` lines from `.env`;
setup asks for household names, language and timezone instead.
Set the AIOStreams secret and public HTTPS address in `.env.aiostreams`.
The empty Skald container waits for setup. Follow its printed next steps.
Use [the setup guide](docs/SETUP.md) for HTTPS and laptop imports.
An agent can use `skald setup --status`, then run the printed next command.

## Recommended: the setup we run

Tested by us. OpenRouter adds natural-language AI search to this setup.
Only AIOStreams needs an HTTPS address. Skald stays inside the Docker network.

```text
Apple TV app (Odin) → AIOStreams (Jellyfin mode, HTTPS) → Skald (internal)
```

1. Run `mkdir -p data && sudo chown 65534 data && sudo chmod 700 data`.
   Skald runs as user 65534 and keeps DATA private.
2. Copy `.env.example` to `.env` and `.env.aiostreams.example` to `.env.aiostreams`.
   Set TMDB and OpenRouter keys in `.env`. Make the AIOStreams secret with
   `openssl rand -hex 32`; set its public HTTPS address in `.env.aiostreams`.
3. Run `docker compose -f compose.stack.example.yaml up -d --build`.
4. Run `docker compose -f compose.stack.example.yaml exec skald skald setup`.
5. Add the printed internal manifest URL as a custom addon in AIOStreams.
6. Add your AIOStreams HTTPS address as a server in Odin. Log in with the AIOStreams
   config UUID as username and the config password.

Stremio can use the same AIOStreams URL; that path should work, not yet tested by us.
AIOStreams is a separate project with its own [docs and licence](https://github.com/Viren070/AIOStreams).
We reference its published image. Configure its stream addons for playback.

## Where you watch

| App | How to connect | What we have tried |
|---|---|---|
| Stremio | Install the printed member or Together URL | Should work, not yet tested by us |
| AIOStreams in Stremio | Add Skald as a custom addon | Should work, not yet tested by us |
| ElfHosted AIOStreams | Add the HTTPS manifest URL | Should work, not yet tested by us |
| Jellyfin apps / Odin on Apple TV | Use AIOStreams' Jellyfin server mode | Tested by us |
| Plex | Keep your Plex playback; optionally import its history | Should work, not yet tested by us |

Follow [the watching recipes](docs/WATCH.md). Keep your stream addons for playback.
For the simple Stremio-direct alternative, copy only `.env`, then run
`docker compose up -d --build` and `docker compose exec engine skald setup --watch stremio`.

## Teach it your taste

| Source | What to use |
|---|---|
| Netflix | Viewing-history CSV; map each profile to a member |
| IMDb | Ratings or watchlist CSV |
| Letterboxd | Export folder with ratings, diary, watched and likes |
| Trakt export | JSON export folder |
| YouTube | Takeout watch-history JSON |
| Chrome | A copy of the browser history database |
| Plain text | A file with one title per line |
| Trakt API | `skald setup --connect trakt` |
| Jellyfin | `skald setup --connect jellyfin` |
| Plex history | `skald setup --connect plex` |
| AIOStreams database | `skald setup --connect aiostreams-db` |

Start with `skald setup --connect files` for imports, or choose a connected source.
See [connectors and their limits](docs/CONNECTORS.md) and [commands by task](docs/CLI.md).

## Search

Title search always works. With OpenRouter, you can also ask for
“cozy mystery for a rainy evening”.

## Privacy

Household history and taste stay on your machine.
TMDB receives title lookups; optional OpenRouter receives taste and history context.
AniList receives anime titles, which can come from your history. Optional NZBHydra receives
titles for availability checks. Configured connectors receive requests for your history.
Keep private addon links, tokens, data and backups private.
Anyone with the household addon link can choose any member's rows.

## Status

Self-hosted, one household per container, one to six members.
Check progress with `docker compose -f compose.stack.example.yaml exec skald skald setup --status`.
Live services and Docker are separate from the offline test suite.
See [backups and upgrades](docs/UPGRADE.md) before updating.

## License

[GNU AGPL-3.0](LICENSE).
This product uses TMDB and the TMDB APIs but is not endorsed, certified,
or otherwise approved by TMDB.
