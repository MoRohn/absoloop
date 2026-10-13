# Deploy absoloop.ai on web-1

The public site is the static tree in [`site/`](site/). On web-1, Caddy runs in Docker (container `caddy`, image `caddy:2`). The host Caddyfile is `/srv/caddy/Caddyfile`, mounted in the container at `/etc/caddy/Caddyfile`, and it is shared with diony.ai. Site files live on the host under `/srv/sites`, mounted read-only in the container at `/srv/sites`.

Deploy rsyncs `site/` to `/srv/sites/absoloop.ai/` on the host. It never reads or writes `/srv/caddy/Caddyfile`. A reload is optional and only re-reads that file inside the container. This repo does not change AWS.

## What production is today

Checked 13 October 2026.

The pages themselves were a static upload on Amazon S3, fronted by CloudFront:

- `server: AmazonS3`, `via: *.cloudfront.net`, `x-cache: Miss from cloudfront`
- HTML `cache-control: no-cache`; CSS and images `public, max-age=300`
- `last-modified: Sun, 19 Jul 2026` for `index.html`, `styles.css`, `robots.txt`, `sitemap.xml`, `assets/absoloop-mark.png`, and `assets/absoloop-logo-hq-capital-l.png`
- Response headers already included HSTS (`max-age=63072000; includeSubDomains; preload`), a strict CSP, `X-Frame-Options: DENY`, `nosniff`, and `Referrer-Policy`

This repository never contained that site or a CloudFront/S3 workflow. [`.github/workflows/ci.yml`](.github/workflows/ci.yml) only runs unit tests. There is nothing CloudFront-related to disable in Actions. The new [`.github/workflows/deploy-web.yml`](.github/workflows/deploy-web.yml) is the deploy path. Do not add AWS credentials to this repo. Leave the S3 bucket and the CloudFront distribution alone until HTTPS on web-1 is serving the new page; then remove them by hand.

DNS, checked the same day against 1.1.1.1 and 8.8.8.8:

| Record | Value |
|---|---|
| NS | `ns1.vercel-dns.com`, `ns2.vercel-dns.com` |
| `absoloop.ai` A | `2.29.59.0` (TTL 60) |
| `www.absoloop.ai` A | `2.29.59.0` (TTL 60) |

`2.29.59.0` reverses to `static.0.59.29.2.clients.your-server.de` (Hetzner). Port 80 on that address answered `Server: Caddy` with a 308 to `https://absoloop.ai/`. Port 443 completed TCP and then aborted the TLS handshake (`alert internal error`, no certificate). Confirm with `dig` before you change anything. If the name already points at web-1, publishing files into `/srv/sites/absoloop.ai` is what the running site block serves. If `dig` shows a CloudFront name instead, do the DNS cutover in step 6 only after `docker exec caddy caddy validate` succeeds.

## 1. Secrets and variables

Repository **secrets** (Settings → Secrets and variables → Actions → Secrets). Do not commit these.

| Name | Required | Value |
|---|---|---|
| `WEB1_HOST` | yes | Hostname or IP of web-1. Not a URL. |
| `WEB1_USER` | yes | SSH user that can create files in `/srv/sites/absoloop.ai`. For an optional reload, that user must be able to run `docker exec` against the `caddy` container (member of the `docker` group). |
| `WEB1_SSH_KEY` | yes | Private key for that user, OpenSSH PEM or OpenSSH format, including the `BEGIN` and `END` lines. The matching public key must already be in that user's `authorized_keys`. |
| `WEB1_KNOWN_HOSTS` | no | One or more lines from `ssh-keyscan -H <WEB1_HOST>`. When set, deploy refuses unknown host keys. |

Repository **variables** (same page, Variables tab). All optional.

| Name | Default | Value |
|---|---|---|
| `WEB1_WEB_ROOT` | `/srv/sites/absoloop.ai` | Dedicated directory on the host. Must match `root` in the site block. Do not set this to `/srv/sites` or anything under `/srv/caddy`. |
| `WEB1_PORT` | `22` | SSH port. |
| `WEB1_CADDY_RELOAD` | unset | Set to `true` only after a hand edit of `/srv/caddy/Caddyfile`. Leave unset for normal publishes. New files show up without a reload. |

A manual run of **Deploy website** has a `reload_caddy` checkbox. That runs `docker exec caddy caddy reload --config /etc/caddy/Caddyfile` on the host. It does not upload a Caddyfile. Use it only after you have edited `/srv/caddy/Caddyfile` yourself.

The same variables work in a shell for [`deploy/deploy.sh`](deploy/deploy.sh). `WEB1_SSH_KEY` may be either the key text or a path to a key file.

## 2. One-time directory on web-1

SSH to web-1 as an admin. Pick the deploy user (the same name you will store in `WEB1_USER`).

```bash
sudo mkdir -p /srv/sites/absoloop.ai
sudo chown "$DEPLOY_USER": /srv/sites/absoloop.ai
sudo chmod 755 /srv/sites /srv/sites/absoloop.ai
```

`/srv/sites/absoloop.ai` must be dedicated to this site. Deploy uses `rsync --delete` inside that directory only. `/srv/sites` is mounted read-only into the container, so publish on the host path, not with `docker cp`.

The deploy script publishes directories as `755` and files as `644` so the `caddy` user in the container can read them.

Confirm the deploy user can log in with the key you will store:

```bash
ssh -i /path/to/web1_key "$DEPLOY_USER@$WEB1_HOST" 'mkdir -p /srv/sites/absoloop.ai && touch /srv/sites/absoloop.ai/.write-test && rm /srv/sites/absoloop.ai/.write-test'
```

## 3. Site block in the shared Caddyfile

Do not replace `/srv/caddy/Caddyfile`. It also serves diony.ai. The deploy script refuses to rsync onto `/srv/caddy` or any path named `Caddyfile`.

Open the host file and confirm this block is present, next to the diony.ai block. The copy in [`deploy/absoloop.caddy`](deploy/absoloop.caddy) is the same text, for reference. If the block is already there, leave the file alone.

```caddyfile
absoloop.ai, www.absoloop.ai {
	root * /srv/sites/absoloop.ai
	encode zstd gzip
	file_server
}
```

Both names serve `/srv/sites/absoloop.ai`. Inside the container that path is the read-only mount of the host directory. There is no separate www redirect.

If you had to add the block by hand, validate and reload inside the container. A failed reload keeps the previous config.

```bash
docker exec caddy caddy validate --config /etc/caddy/Caddyfile
docker exec caddy caddy reload --config /etc/caddy/Caddyfile
```

`/etc/caddy/Caddyfile` in that command is the container path. It is the mount of `/srv/caddy/Caddyfile`. Do not point reload at a different file.

## 4. Optional reload from GitHub Actions

Skip this for normal publishes. Replacing HTML and assets does not need a reload.

The reload checkbox and `WEB1_CADDY_RELOAD=true` run this on the host, and nothing else:

```bash
docker exec caddy caddy reload --config /etc/caddy/Caddyfile
```

`WEB1_USER` must be allowed to talk to the Docker socket:

```bash
sudo usermod -aG docker "$DEPLOY_USER"
```

Log in again after that so the group applies. Do not grant the deploy user write access to `/srv/caddy/Caddyfile`.

## 5. Publish the files

Either push to `main` (paths under `site/`, `deploy/`, or the deploy workflow) or run **Deploy website** from the Actions tab.

From a checkout, the same script the workflow runs:

```bash
export WEB1_HOST="…"
export WEB1_USER="…"
export WEB1_SSH_KEY="$(cat /path/to/web1_key)"
# export WEB1_CADDY_RELOAD=1   # only when the Caddyfile changed
bash deploy/deploy.sh
```

## 6. DNS cutover

Nameservers are Vercel DNS. Change records there, not in Route 53, unless `dig NS absoloop.ai` says otherwise on the day you cut over.

1. `dig +short absoloop.ai A` and `dig +short www.absoloop.ai A`.
2. If both already point at web-1, do not add a second record. Finish steps 2–5 and go to step 7. If you changed the shared Caddyfile, reload with `docker exec caddy caddy reload --config /etc/caddy/Caddyfile`. Port 80 has to reach the `caddy` container so HTTP-01 can complete.
3. If they still point at CloudFront, lower the TTL if it is not already 60 seconds. After `docker exec caddy caddy validate --config /etc/caddy/Caddyfile` succeeds, replace the apex and `www` records with A (and AAAA, if web-1 has IPv6) records for web-1. Remove the CloudFront alias. Do not delete the CloudFront distribution yet.
4. Wait for the TTL. `dig @1.1.1.1 absoloop.ai A` should show web-1.

Ports 80 and 443 on web-1 must reach Caddy. Certificate issuance uses those ports. This repo does not open them for you.

## 7. Check the new site

```bash
curl -sI https://absoloop.ai | head
curl -sI https://www.absoloop.ai | head
curl -sI https://absoloop.ai/no-such-page | head
```

Expect:

- `https://absoloop.ai` returns `200` and the new HTML (the hero image URL is `/assets/absoloop-os.webp`)
- `https://www.absoloop.ai` returns `200` for the same site (both names are one site block)
- an unknown path returns `404`
- the certificate covers `absoloop.ai` and `www.absoloop.ai`

If TLS still aborts with no certificate:

```bash
docker logs caddy --tail 80
docker exec caddy caddy validate --config /etc/caddy/Caddyfile
```

Typical causes: the absoloop block is missing from `/srv/caddy/Caddyfile`, port 80 does not reach this container so HTTP-01 never completes, or a broken certificate is cached for these names. Edit only the absoloop block. Do not restart the container in a way that drops diony.ai.

## 8. Retire CloudFront later

After the checks above pass, remove the CloudFront distribution and the S3 origin in the AWS console when you are ready. Nothing in this repository talks to AWS. Keeping the distribution unused for a few days is a rollback: point the Vercel DNS A records back at it.

## Hero asset

The centerpiece is [`site/assets/absoloop-os.webp`](site/assets/absoloop-os.webp), rebuilt from the README GIF [`docs/assets/AbsoLoopOS.gif`](docs/assets/AbsoLoopOS.gif): near-black keyed to transparent, 12 fps, animated WebP. [`site/assets/absoloop-os-still.webp`](site/assets/absoloop-os-still.webp) is the fullest frame, shown when the visitor asks for reduced motion. Regenerate with `python3 deploy/encode_hero.py` (needs `ffmpeg` and `Pillow`).
