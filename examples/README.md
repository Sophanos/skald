# Pick a Compose setup

Start at the root of a Skald checkout on your Docker host. Install Docker with Compose first.
Choose one folder, then run its commands in order. Each folder is a separate deployment.
Before `up`, fill the required settings in the private env files using the editor.
Get a TMDB API key from TMDB account settings. OpenRouter is optional; leave it blank to disable AI.
Replace reserved example hostnames with your own addresses. Never put secrets in command arguments.

| Setup | Who it is for | What you need | Commands from checkout to first rows |
|---|---|---|---|
| [stremio](stremio/) | Stremio or another addon app on your LAN | TMDB key, host LAN address | 1. `cd examples/stremio && cp env.example .env && chmod 600 .env && vi .env`<br>2. `mkdir -p data && sudo chown 65534 data && sudo chmod 700 data`<br>3. `docker compose up -d --build`<br>4. `docker compose exec skald skald setup --watch stremio` |
| [odin-jellyfin](odin-jellyfin/) | Odin on Apple TV or Jellyfin apps | TMDB key, host LAN address, OpenSSL | 1. `cd examples/odin-jellyfin && cp env.example .env && sed '/^SECRET_KEY=/d' aiostreams.env.example > .aiostreams.env && chmod 600 .env .aiostreams.env && openssl rand -hex 32 \| sed 's/^/SECRET_KEY=/' >> .aiostreams.env && vi .env .aiostreams.env`<br>2. `mkdir -p data && sudo chown 65534 data && sudo chmod 700 data`<br>3. `docker compose up -d --build`<br>4. `docker compose exec skald skald setup --watch jellyfin-app --connect files`<br>5. `docker compose exec skald skald setup aiostreams` |
| [existing-aiostreams](existing-aiostreams/) | You already host AIOStreams in Docker | TMDB key, existing shared Docker network | 1. `cd examples/existing-aiostreams && cp env.example .env && chmod 600 .env && vi .env compose.yaml`<br>2. `mkdir -p data && sudo chown 65534 data && sudo chmod 700 data`<br>3. `docker compose up -d --build`<br>4. `docker compose exec skald skald setup --watch jellyfin-app --connect files`<br>5. `docker compose exec skald skald setup aiostreams` |
| [reverse-proxy](reverse-proxy/) | Addon apps reaching Skald over public HTTPS | TMDB key, public hostname, ports 80 and 443 | 1. `cd examples/reverse-proxy && cp env.example .env && chmod 600 .env && vi .env Caddyfile`<br>2. `mkdir -p data && sudo chown 65534 data && sudo chmod 700 data`<br>3. `docker compose up -d --build`<br>4. `docker compose exec skald skald setup --watch stremio` |
| [tunnel](tunnel/) | Public HTTPS without opening router ports | TMDB key, Cloudflare account and domain, named tunnel | 1. `cd examples/tunnel && cp env.example .env && cp tunnel.env.example .tunnel.env && chmod 600 .env .tunnel.env && vi .env .tunnel.env`<br>2. `mkdir -p data && sudo chown 65534 data && sudo chmod 700 data`<br>3. `docker compose up -d --build`<br>4. `docker compose exec skald skald setup --watch stremio` |
| [small-box](small-box/) | Raspberry Pi or a NAS, with bounded resources | TMDB key, 64-bit Docker host, LAN address | 1. `cd examples/small-box && cp env.example .env && chmod 600 .env && vi .env`<br>2. `mkdir -p data && sudo chown 65534 data && sudo chmod 700 data`<br>3. `docker compose up -d --build`<br>4. `docker compose exec skald skald setup --watch stremio` |

Setup asks for household members, language, timezone, country and a taste source.
The container runs as user 65534. Its private `./data` folder is mounted at `/data`.
Empty Skald reports unhealthy with `not-configured` until setup saves the household;
it starts automatically afterward. Allow several minutes for the first rows to build.
Install the printed member or Together addon link; for Odin use the printed server, user and password.
Keep these links private. Keep your stream addons for playback; Skald supplies rows and search.
After changing env files, use `docker compose up -d --force-recreate`.
After changing connectors, use `docker compose restart skald`.
Back up private env files and data before upgrades; see [upgrade instructions](../docs/UPGRADE.md).
Here all services use the name `skald`; run Compose inside your chosen folder.

## stremio

At the address prompt, enter `http://192.0.2.10:8080`, replacing the documentation IP
with the host's LAN address. Port 8080 is published for LAN clients; do not forward it on your router.
Clients requiring HTTPS should use reverse-proxy or tunnel instead.
To learn from Stremio, choose its connector at setup and sign in privately for each member.

## odin-jellyfin

The first command generates the encryption key directly into the private AIOStreams env file.
Set `BASE_URL` there to `http://192.0.2.10:3000`, replacing the IP with your host's LAN address.
Port 3000 serves LAN apps. Skald has no published port; `SKALD_INTERNAL_BASE=http://skald:8080`
lets AIOStreams fetch its private addon inside Docker. Setup reaches AIOStreams at `http://aiostreams:3000`.
For remote apps, provide an HTTPS proxy or tunnel for AIOStreams and change `BASE_URL` to that address.
Wait for Skald rows and AIOStreams startup before the final login command; retry it if either is still starting.
Configure stream addons in your new AIOStreams login, then sign in from Odin.
The AIOStreams database volume persists at `/app/data` and is also mounted read-only at `/aio-data`.
After a first play, run `docker compose exec skald skald setup --connect aiostreams-db --source-path /aio-data/db.sqlite`,
map profiles to members or `together`, then restart Skald. The directory, database, WAL and SHM files
must be readable by user 65534; see [connector instructions](../docs/CONNECTORS.md#aiostreams-database).
Back up the AIOStreams volume too.

## existing-aiostreams

In `compose.yaml`, change network `name: aio` to the network your AIOStreams already uses.
It must be a user-defined Docker network with a reachable `aiostreams` service name or alias.
If its name or port differs, give the final command `--aio-url http://YOUR_SERVICE_NAME:3000`.
Set your existing AIOStreams `BASE_URL` to the player-reachable address and enable `JELLYFIN_ENABLED` for Odin.
For Stremio via AIOStreams, use `--watch aiostreams` in the household command instead.
The final command makes a new login; to update one, add `--uuid YOUR_LOGIN_UUID` and enter its password privately.
Skald publishes no ports and advertises `http://skald:8080` inside the shared network.
For Odin history, add your existing database directory or volume as `/aio-data:ro` under Skald volumes,
including WAL and SHM files, then use the connector command above. Leave its existing database in place.

## reverse-proxy

Replace `skald.example.org` in `Caddyfile` with your public hostname and point DNS at this host.
Forward public TCP ports 80 and 443 to it; Caddy obtains and renews certificates automatically.
At setup's address prompt use `https://skald.example.org` with your real hostname.
Caddy forwards to `skald:8080`; Skald has no published port. Certificate state stays in named volumes.
Keep request access logging off and private addon paths out of proxy error logs.

## tunnel

Create a remotely managed named tunnel in Cloudflare and paste its token into `.tunnel.env`.
Add a public hostname route in the Cloudflare dashboard with service URL `http://skald:8080`.
At setup's address prompt, use your routed HTTPS address, such as `https://skald.example.org`.
No service publishes a port. Cloudflared connects outward; allow outbound access to Cloudflare.
An interactive Cloudflare Access login in front of the addon can prevent players from connecting.

## small-box

Use the stremio LAN address instructions. Skald is limited to 768 MiB and one CPU.
Initial builds, large imports and rebuilding title caches take longer, especially on an SD card.
Prefer SSD storage. If the container is killed for lack of memory, raise `mem_limit` before retrying.
Leave OpenRouter blank to skip AI work. Docker still needs host memory outside this container limit.

AIOStreams uses the root stack's digest pin. Caddy and cloudflared use version tags because
their digests could not be verified offline. These examples have offline checks; live deployments need network access.
