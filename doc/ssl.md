# TLS / HTTPS for opp_ci

`opp_ci serve` is a regular uvicorn app. There are three ways to put
HTTPS in front of it; this doc covers all three and points you at the
right one for your situation.

| Pattern | When to pick it |
|---|---|
| **Cloudflare Origin Certificate** | You already use Cloudflare for the DNS/CDN of the public hostname. Simplest end-to-end. Recommended. |
| Reverse proxy (Caddy / nginx + Let's Encrypt) | You run other web services on the same VM and want one TLS termination point in front. |
| Native TLS + ACME (certbot / acme.sh) | One service per VM, no Cloudflare, want a public cert. |
| Self-signed (`opp_ci tls-selfsign`) | Lab / dev / smoke-test only. |

The first three are production-grade. The fourth exists so a developer
can verify the TLS code paths without going through a CA. Don't ship
self-signed to users.

## Recommended: Cloudflare Origin Certificate

Traffic flows **browser → Cloudflare edge → opp_ci origin**. The
browser sees Cloudflare's publicly trusted edge cert; the origin
presents a **Cloudflare Origin Certificate** (issued by Cloudflare's
Origin CA, not in any public trust store, valid up to 15 years).

### Cloudflare-side setup

1. In the dashboard for the zone (e.g. `ci.omnetpp.org`):
   **SSL/TLS → Origin Server → Create Certificate**.
   - Hostnames: the public hostname (and optionally `*.<zone>`).
   - Type: RSA-2048 or ECDSA.
   - Validity: 15 years (the maximum).
2. The dashboard shows the cert PEM and key PEM **once**. Copy both
   immediately to the origin host.
3. **SSL/TLS → Overview → mode = Full (strict).** Anything less defeats
   the point — Cloudflare won't verify what the origin presents, and
   "Flexible" mode does plain HTTP over the edge↔origin leg.

### Origin-side setup

```bash
# On the origin host, paste the two PEMs from the Cloudflare dashboard:
sudo install -m 0640 -o root -g opp_ci /dev/stdin /etc/opp_ci/tls/privkey.pem    <<< '...key PEM...'
sudo install -m 0640 -o root -g opp_ci /dev/stdin /etc/opp_ci/tls/fullchain.pem  <<< '...cert PEM...'
```

Edit `/etc/opp_ci/serve.env` — uncomment:

```
OPP_CI_SERVE_TLS_CERT_FILE=/etc/opp_ci/tls/fullchain.pem
OPP_CI_SERVE_TLS_KEY_FILE=/etc/opp_ci/tls/privkey.pem
OPP_CI_PUBLIC_URL=https://ci.omnetpp.org
OPP_CI_SERVE_HOST=0.0.0.0
OPP_CI_SERVE_PORT=443
```

Activate the systemd drop-in (it grants `CAP_NET_BIND_SERVICE` for
port 443) and the cert-watching `.path` unit:

```bash
sudo mv /etc/systemd/system/opp_ci-serve.service.d/tls.conf.example \
        /etc/systemd/system/opp_ci-serve.service.d/tls.conf
sudo systemctl daemon-reload
sudo systemctl enable --now opp_ci-serve-cert.path
sudo systemctl restart opp_ci-serve.service
```

Verify:

```bash
# Via Cloudflare's edge (publicly trusted, no --cacert needed):
curl https://ci.omnetpp.org/api/health

# Direct to origin (loopback resolve + Cloudflare's Origin CA bundle):
curl --resolve ci.omnetpp.org:443:127.0.0.1 \
     --cacert /etc/opp_ci/tls/cloudflare-origin-ca.pem \
     https://ci.omnetpp.org/api/health
```

Both should return 200.

### Workers and Python clients

| Connector | Endpoint | CA bundle? |
|---|---|---|
| Worker / `OppCiClient` using `https://ci.omnetpp.org` | Cloudflare edge | **no** — system CA store works |
| Same, pointing at the origin IP/internal DNS (bypass Cloudflare) | origin Origin Cert | **yes** — `OPP_CI_TLS_CA_BUNDLE=/etc/opp_ci/tls/cloudflare-origin-ca.pem` |

Default recommendation: **route workers through Cloudflare's edge** —
set `OPP_CI_COORDINATOR_URL=https://ci.omnetpp.org` in their env file.
No `CA_BUNDLE` config needed, and the edge's DDoS protections apply to
worker traffic.

The shipped `/etc/opp_ci/tls/cloudflare-origin-ca.pem` is a bundle of
both Cloudflare Origin CA roots (RSA + ECC), written by
`opp_ci serve service install`. Refresh it from
<https://developers.cloudflare.com/ssl/origin-configuration/origin-ca/>
if Cloudflare rotates the root (rare).

### Lock the origin to Cloudflare's IP ranges

The Origin Cert alone doesn't stop someone from connecting directly to
the origin IP — they'll get a TLS warning, but the port is open.
Recommended: firewall to Cloudflare's published ranges:

```bash
# Replace these with the current ranges from https://www.cloudflare.com/ips/
sudo ufw allow from 173.245.48.0/20 to any port 443 proto tcp
sudo ufw allow from 103.21.244.0/22 to any port 443 proto tcp
# ... add all published ranges, IPv4 and IPv6
sudo ufw enable
```

If you also run a same-host worker connecting to `127.0.0.1`, add
`sudo ufw allow from 127.0.0.0/8` (or skip — loopback isn't filtered
by default with `ufw`).

Cloudflare rotates IP ranges occasionally; a cron job that pulls the
list weekly and rebuilds the ruleset is the operator's responsibility.

### Renewal

15-year validity means the renewal flow is dormant essentially
forever. When you do rotate (key compromise, hostname change), the
flow is:

1. Generate a new cert in the Cloudflare dashboard.
2. Overwrite `/etc/opp_ci/tls/privkey.pem` *first*, then
   `/etc/opp_ci/tls/fullchain.pem`. (Order matters — see "Renewal
   edge case" below.)
3. The shipped `opp_ci-serve-cert.path` unit notices and triggers
   `opp_ci-serve-cert-reload.service`, which runs
   `systemctl restart opp_ci-serve.service`.

Active HTTP sessions get one TLS reconnect (~ms). Workers retry on the
next poll. No manual `systemctl restart` needed.

## Alternative: reverse proxy with HTTPS

Run Caddy or nginx + Let's Encrypt on the same host, keep `opp_ci serve`
on `127.0.0.1:8080`. The proxy terminates TLS and forwards plain HTTP
on loopback.

This is the right call if you also host other web services on the VM,
or if your ops setup already has a proxy.

Caddyfile example:

```
ci.example.org {
    reverse_proxy 127.0.0.1:8080
}
```

In this shape, leave the TLS env vars in `serve.env` **unset** and
leave the drop-in as `.example`. Just set
`OPP_CI_PUBLIC_URL=https://ci.example.org` (so OAuth callbacks point
at the public hostname) and
`OPP_CI_SESSION_COOKIE_SECURE=1` (since the browser sees HTTPS even
though the origin doesn't).

## Alternative: native TLS with public ACME (certbot / acme.sh)

Use this when you want a publicly trusted cert on the origin but
aren't behind Cloudflare and don't want a reverse proxy.

Same env-var + drop-in setup as the Cloudflare path above; the
difference is where the cert files come from. After the initial
issuance, ACME clients renew automatically. The shipped
`opp_ci-serve-cert.path` unit handles the restart-on-renewal step,
*provided* the renewed file ends up at
`/etc/opp_ci/tls/fullchain.pem` (and `privkey.pem`) as a regular file,
not a symlink.

### certbot

Run certbot once to obtain the cert (`--standalone`, `--webroot`, or
`--dns-…` mode — your choice). Then drop a deploy hook that copies
the renewed files into place:

```bash
sudo tee /etc/letsencrypt/renewal-hooks/deploy/opp_ci.sh > /dev/null <<'EOF'
#!/bin/sh
LINEAGE=/etc/letsencrypt/live/ci.example.org
install -m 0640 -o root -g opp_ci "$LINEAGE/privkey.pem"    /etc/opp_ci/tls/privkey.pem
install -m 0640 -o root -g opp_ci "$LINEAGE/fullchain.pem"  /etc/opp_ci/tls/fullchain.pem
EOF
sudo chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/opp_ci.sh
```

You don't need `systemctl restart` in the hook — the `.path` unit
catches the cert change. Symlinking from
`/etc/opp_ci/tls/fullchain.pem` into the letsencrypt tree doesn't
work: `PathChanged=` watches the symlink target's path, but renewal
only rewrites the *symlink*. Always copy.

### acme.sh

```bash
acme.sh --install-cert -d ci.example.org \
    --key-file       /etc/opp_ci/tls/privkey.pem \
    --fullchain-file /etc/opp_ci/tls/fullchain.pem
```

acme.sh writes the files in the right order (key first, then chain),
so the `.path` watcher sees a consistent pair. No `--reloadcmd`
needed.

## Alternative: self-signed for lab use

For a developer laptop or smoke-test VM, generate a self-signed cert:

```bash
sudo opp_ci tls-selfsign \
    --host ci.lab.local \
    --extra-san 10.0.0.5 \
    --out /etc/opp_ci/tls
```

The SAN list always includes `--host`, `localhost`, `127.0.0.1`, and
the machine's hostname, so a same-host worker doesn't hit a name
mismatch.

Workers connecting to this coordinator need to trust the cert. Two
options:

```
# In /etc/opp_ci/workers/<name>.env:
OPP_CI_TLS_CA_BUNDLE=/etc/opp_ci/tls/fullchain.pem
# or, dev-only:
OPP_CI_TLS_INSECURE=1
```

`tls-selfsign` is not for production. Workers refuse the cert by
default, and browsers show a warning every time.

## Key handling: option A vs option B

The shipped drop-in (`/etc/systemd/system/opp_ci-serve.service.d/tls.conf`)
supports two key-protection modes:

**Option A (default).** `/etc/opp_ci/tls/privkey.pem` is mode `0640`,
group `opp_ci`. The service reads it directly.

**Option B (hardened, systemd ≥ 250).** `privkey.pem` stays
`0600 root:root`; systemd copies it into a per-unit tmpfs at
`$CREDENTIALS_DIRECTORY/privkey.pem` on each start. The service can't
read the on-disk key at all.

To enable option B:

1. In the drop-in, uncomment:
   ```
   LoadCredential=privkey.pem:/etc/opp_ci/tls/privkey.pem
   ```
2. In `serve.env`, change:
   ```
   OPP_CI_SERVE_TLS_KEY_FILE=${CREDENTIALS_DIRECTORY}/privkey.pem
   ```
3. Tighten the key file: `sudo chmod 0600 /etc/opp_ci/tls/privkey.pem
   && sudo chown root:root /etc/opp_ci/tls/privkey.pem`.
4. `sudo systemctl daemon-reload && sudo systemctl restart opp_ci-serve.service`.

Ubuntu 22.04 ships systemd 249 (no `LoadCredential=`); use option A
there. Ubuntu 24.04+ has it.

## Renewal edge case: key/cert ordering

The `.path` watcher fires on changes to `fullchain.pem`. If you
overwrite `fullchain.pem` *before* `privkey.pem`, the service may
restart with mismatched halves and crash on the first handshake.

**Always write the key first, then the cert.**
`opp_ci tls-selfsign`, `certbot --deploy-hook` (the shipped recipe
above), and `acme.sh --install-cert` all do this correctly.

## Five-line laptop recipe

For poking at the TLS code paths on your dev machine:

```bash
sudo opp_ci tls-selfsign --host localhost --out /etc/opp_ci/tls
export OPP_CI_SERVE_TLS_CERT_FILE=/etc/opp_ci/tls/fullchain.pem
export OPP_CI_SERVE_TLS_KEY_FILE=/etc/opp_ci/tls/privkey.pem
export OPP_CI_TLS_INSECURE=1   # for the worker / Python client
opp_ci serve --host 127.0.0.1 --port 8443
```

Then `curl --insecure https://127.0.0.1:8443/` or, with verification:
`curl --cacert /etc/opp_ci/tls/fullchain.pem https://localhost:8443/`.
