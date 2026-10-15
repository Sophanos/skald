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

## Leaving, guests and forgetting

You can stop someone's influence on household picks without erasing their history.
Removing a person keeps their saved data and can be undone. Away keeps an adult's
profile and link working, while their taste stops shaping other people's picks.
Guests' reactions shape their own picks and shared guest shelves, not household taste.
When a visit expires, the guest leaves the active household and their shelf stops
showing; their history and ratings stay saved. A shelf with its own end date is removed
when that date expires. Guest expiry is not erasure.

"Forget Ben" in search only previews permanent erasure and shows the server command.
Erase runs only from the server command line:
`python -m skald members forget ben --person ana`. Preview first, then repeat with
`--yes` or `--plan HASH` to apply. Search confirmation codes and the HTTP command API
cannot erase a person. It currently accepts adult members, not children or named guests.
It removes that adult's personal history, ratings, imports, history summaries,
preferences, names, shelves (including shared shelves), personal row ideas, saved
commands, memory and interview answers, title corrections and linked clients.
Their sync accounts, saved sync data and credentials no longer used by another
account are removed. Their personal and Together row metrics are erased, derived
taste and row caches are cleared, and household row-play learning starts again.
Other people's own ratings and data stay.

Together-watches through the forget date stay seen for the others but stop teaching
their taste. A later watch or someone's own rating can teach taste again. Skald keeps
a local cutoff for the forgotten id to prevent old syncs from bringing their evidence
back. After forgetting, no information about that person is sent anywhere;
their inputs are excluded from later learning and sync results. It cannot retract
requests already sent to services before the forget.

Backups made before forgetting still contain the old data. Daily archives keep the
newest 14 distinct backup dates. Among the remaining older dates, they also keep
the oldest saved date in each of up to eight most recent ISO calendar weeks represented.
They are pruned as newer backups replace them, not after a fixed number of days.
The newest three pre-migration archives are
kept separately. Private `backups/pre-people-*.json` settings copies, including the
one made before forgetting, have no automatic expiry. Forget does not rewrite any
of these backups; remove old copies yourself if they must not retain the person.

## Keep it private

Keep addon links, tokens, data and backups private.
Anyone with the household addon link can choose any member's rows.
See [who can access the data](CONNECTORS.md#who-can-access-the-data) and [health alerts](SETUP.md#optional-health-checks-and-alerts).
