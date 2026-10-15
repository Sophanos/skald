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

## Children's watching days

Current adults in the household may read a child's plays, likes and watching days.
Guests and away adults cannot read a child's data. Children and guests read only
their own plays and taste. Watching days are available only to current adults.
Adults still cannot read each other's plays. Reading a child's taste gives no
correction previews or extra permission to change it.

The private `watching-days.json` file keeps only child ids and dates with at least
one play from that child's own mapped Odin/AIOStreams login. It contains no titles,
times or logins. A shared or household login never creates a child's watching day.
Skald does not guess who watched. Bulk backfill times do not count as watching days.
Days start at 05:00 in the household time zone. The rolling window is 56 days,
independent of raw play retention, and older dates are pruned on plays reads,
retention cleanup and settings reloads. Adults see dates and counts, with no claim
about holidays, visits or where a child is staying.

Only children have entries. Making a child an adult removes their day list.
Forgetting a child removes their watching days and counts them in the preview.
Forgetting history before a cutoff date removes earlier watching days and keeps
the cutoff day and later days. Whole-household erase deletes the file.

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
cannot erase a person. A current adult can erase an adult, child or named guest,
including someone already removed. The adult cannot be away or erase themselves.
It removes that person's personal history, ratings, imports, history summaries,
preferences, names, shelves (including shared shelves), personal row ideas, saved
commands, memory and interview answers, title corrections and linked clients.
Their sync accounts, saved sync data and credentials no longer used by another
account are removed. Their personal and Together row metrics are erased, derived
taste and row caches are cleared, and household row-play learning starts again.
Other people's own ratings and data stay.

Shared watches keep teaching the people who stay, with the erased name removed.
Add `--shared drop` to remove those watches too; their titles stay seen for the others
but stop teaching their taste. The preview counts shared watches per remaining person.
A later watch or someone's own rating can teach taste again. Skald keeps
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

Skald removes its saved copy of the person's AIOStreams login and its login mapping.
When either existed, the preview and result remind you: "Ben's Odin login and its
watch history still exist in Odin and AIOStreams. Remove them there if Ben should
lose access." Skald does not delete accounts or history in those services.

To erase everyone, run `python -m skald erase --household --json` on the server.
Review the counts, then repeat with `--yes --plan CODE --confirm ERASE`. Without
`--json`, a terminal can ask you to type ERASE instead. Both confirmations are required.
This erases every personal DATA file, unknown files, credentials and all backups
inside DATA, including daily, pre-people and migration archives. It keeps addon
and server remote tokens, the format marker, setup addresses, stable lock files
and an empty generation marker that blocks stale server writes. Addon links answer
with empty rows; run `skald setup household`, then restart Skald. Environment API keys
stay unchanged. Backups outside Skald's data folder are not touched. Delete them yourself.

## Keep it private

Keep addon links, tokens, data and backups private.
Anyone with the household addon link can choose any member's rows.
See [who can access the data](CONNECTORS.md#who-can-access-the-data) and [health alerts](SETUP.md#optional-health-checks-and-alerts).
