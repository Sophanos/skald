# Changelog

Every future release entry includes a **Data changes:** line stating the data
format, migrations, and any backup or rollback steps. Use “none” when the format
and layout stay the same.

## 1.3.0

Each member can set a kids age with `skald setup prefs --kids-age N|off`; their rows, search and details then show only titles certified for that age, and unknown or adult titles stay hidden.
Households with a child get a Family row picked for the youngest child's age; Together rows learn from adult plays only.
Shelves are standing rows with optional weekday and time windows: up to 10 per person and for Together (4 and 2 by default), managed with `skald shelf` or from search.
Search can change settings in plain words; each change shows a preview card and applies only after its exact "yes NN" code, with undo and a help card.
`skald suggest` and search cards offer shelves based on repeated searches and attributed plays.
Command cards in Odin lead with their code and a readable name and open to a details page.
After a shelf or row change Skald asks AIOStreams to reload rows at once for logins made by `skald setup aiostreams`.
`skald setup rows KEY,...` chooses which Home rows show, in Skald's usual order; two opt-in movie rows, Latest Digital Releases and Top Rated Movies, are new, and `skald setup rows all` restores the defaults.
In a shared AIOStreams database, `--map PROFILE=skip` ignores another household's profile.
`skald import trakt` reads the Trakt export zip directly, and Trakt favorites count as loved.
`skald setup --connect` now says when Skald needs a restart to start reading, and status shows a pending restart even while other steps remain.
`skald setup aiostreams --uuid` replaces a custom Skald preset that points at another engine instead of adding a second one.

**Data changes:** no format bump or migration (DATA format stays 1); adds optional `kids_age` and `shelves` member preferences and `household.shelves` and `household.rows` in `skald.json`, new private `shelves.json` and `commands.json`, and a person field in `searches.jsonl`.
Version 1.2.0 ignores the household keys but rejects `kids_age` or `shelves` member preferences; clear them with `skald setup prefs --person NAME --clear` before rolling back. Child links are not age-filtered on 1.2.0.

## 1.2.0

Each member can hide movies, series, documentaries, animation or anime with `skald setup prefs --hide`, `--show` and `--clear`.
Hiding movies or series affects only that member's own rows; documentaries, animation and anime stay hidden in Together if any member hides them.
Each member can focus on genres, languages and countries; focus moves matching titles up in their own rows and never removes others.
Next episodes ignore these settings; search stays unfiltered.
AI prompts no longer assume two members are a couple or use a gendered default, and the household note now reaches the This Week and mood picks.
The README explains what changes as you watch, and the agent skill covers connecting sources, AIOStreams, adopt and personal settings.

**Data changes:** no format bump or migration; adds optional `hide` and `focus` member preferences in `skald.json` and an origin country field in cached title facts, filled in small batches only when a member focuses on countries.
Version 1.1.0 rejects configs with `hide` or `focus` set; clear them with `skald setup prefs --person NAME --clear` before rolling back.

## 1.1.0

AI search can exclude watched titles or find matches in your own history, including member and Together links.
Connect Stremio per person to learn from watched titles, library interests and loved/liked titles.
Set each member's discovery style, release years and excluded movie or series genres with `skald setup prefs`.
Together rows respect everyone's genre exclusions; settings do not filter search.
Taste lanes can show More from a director or series creator behind at least two titles a member liked or finished.
Together views show these rows only when every member qualifies.
A director or creator row needs 8 unseen titles, so a typical filmography is enough.
The README now opens with what Skald does at home and in search, with pictures from a test household.

**Data changes:** no format bump or migration; adds private Stremio keys and per-person history files, like MDBList, and optional member preferences in `skald.json`.
Version 1.0.9 can read configs with preferences set, but ignores them.

## 1.0.9

`skald setup adopt --yes` makes an older household editable while preserving its config and catalog ids.

**Data changes:** no format change or migration; adoption adds private `skald.json`
and `.skald-household`. Remove both to roll back to the retained `legacy.json`.

## 1.0.8

Connect MDBList per person to read watched history and watchlists with a free API key.
Setup shows each connection and can remove its private key.

**Data changes:** none. MDBList keys are stored privately in DATA like Trakt credentials.

## 1.0.7

A second `skald import` no longer erases titles still waiting for review from an earlier import.

**Data changes:** none.

## 1.0.6

Large YouTube, Chrome and Trakt exports use less memory when imported in the container.
Piped imports are recognised as new files when their contents change.
Piped and Chrome imports briefly use free disk space in the data folder for a temporary copy.

**Data changes:** none.

## 1.0.5

`skald import youtube` now reads the Takeout watch history as HTML, which is
what Google Takeout gives you by default, as well as JSON. You no longer need to
redo the export just to pick JSON.

**Data changes:** none.

## 1.0.4

Rows that come back no longer land at the bottom in AIOStreams. Once you have
reordered or hidden a row on the AIOStreams page, run `skald setup aiostreams`
again: it puts every Skald row back in Skald's order, in the place Skald's rows
already hold. Rows of other addons stay where they are, and rows you hid stay
hidden. Nothing is saved when nothing changed.

`skald setup --status` asks for a restart only while the running engine still
has older settings. A household made before `skald setup` now gets a status
that shows what it can see (TMDB key present, ratings per member) instead of
an error. `skald doctor` suggests importing a Netflix or IMDb export next to
the interview for new members.

**Data changes:** one small file, `.skald-startup`, records which settings the
engine loaded at start. No migration. Going back to 1.0.3 ignores it.

## 1.0.3

Importing on a laptop with `--remote` now matches titles through your own Skald
server, so the laptop needs no TMDB key. Only titles, years and ids are sent,
and `--dry-run` works the same way. `--resolve-online` still works without the
server.

A laptop token made for one member now pushes that member's titles and says
how many it skipped for others, instead of failing the whole push. A refused
push says why.

Watch Again only shows titles you have watched or rated. A new household no
longer gets a row of suggestions it never watched.

**Data changes:** none. New API routes for title matching and token checks.
Update the laptop and the server together.

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
