# How Skald handles your data

Household history, taste, ratings and settings are stored on your machine, in DATA.
Skald runs as user 65534 and keeps DATA private. Credentials use private files and prompts.

## What leaves your machine

These requests leave it:

| Destination | What it receives |
|---|---|
| TMDB | Title searches, title and ID lookups, recommendations and metadata requests. Watching apps and the taste page load TMDB artwork. |
| OpenRouter, if enabled | Taste and history context, title metadata and overviews for embeddings and tagging, search requests and unclear import candidates. |
| AniList | Anime queries and titles, possibly from your history. |
| NZBHydra, if configured | Documentary shelf titles (IDs or names) for availability checks. |
| AIOStreams | Your login and its addon settings: Skald's address and row names, from setup and shelf changes. |
| Stremio, Trakt, MDBList, PublicMetaDB, if connected | That member's key or login, and requests for their history, ratings and lists. |
| Jellyfin or Plex, if connected | Your server token and history requests. The AIOStreams database connector reads a local file. |
| Telegram or a webhook, if you enable alerts | Health messages, without account or row names. |
| Your Skald server, from a linked laptop | Remote previews send title candidates (title, year, kind, external IDs); pushing sends resolved signals. |

Raw exports stay on the laptop. Remote previews send title candidates to your Skald server; pushing sends resolved signals.

## Keep it private

Keep addon links, tokens, data and backups private.
Anyone with the household addon link can choose any member's rows.
See [who can access the data](CONNECTORS.md#who-can-access-the-data) and [health alerts](SETUP.md#optional-health-checks-and-alerts).
