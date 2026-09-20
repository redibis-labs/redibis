# Dashboard authentication, roles, and TLS

Operator guide for the FastAPI dashboard (`redibis.webapp.backend`).
Scan engine packages are unchanged; this is the UI layer only.

Roles: **admin** (full) and **explorer** (read-only). Default-deny middleware
protects every route that is not on an explicit public allowlist.

## Authorization boundary

| Surface | Authorization | Enforced by |
|---|---|---|
| **HTTP API** | Session cookie + CSRF + role | `AuthMiddleware` |
| **Dashboard UI** | Same | Same |
| **CLI** | **None — OS-level trust** | Filesystem permissions |

The CLI (`redibis pii text`, `redibis scan`, …) constructs services in-process.
It never calls the dashboard HTTP API and never goes through middleware.
Anyone who can run the `redibis` binary has full operator capability on that
host. The controls that matter for the CLI are:

- filesystem permissions on `configs/auth/` (the users file is `0o600`; keep it)
- credentials for the contracts/runs buckets
- who has shell access to the host

If a remote or multi-tenant CLI ever appears, this boundary changes and the CLI
would need its own token. Not now.

---

## Configuration

```yaml
auth:
  enabled: true              # false only for local development
  backend: json              # json | db
  store_path: ./configs/auth/users.json
  db_url: ""                 # required when backend: db
  session_hours: 12
  bootstrap_admin_user: ""
  bootstrap_admin_password: ""
```

| Setting | Env | Default |
|---|---|---|
| `auth.enabled` | `REDIBIS_AUTH_ENABLED` | `true` |
| `auth.backend` | `REDIBIS_AUTH_BACKEND` | `json` |
| `auth.store_path` | `REDIBIS_AUTH_PATH` | `./configs/auth/users.json` |
| `auth.db_url` | `REDIBIS_AUTH_DB` | — (required for `db`) |
| `auth.session_hours` | `REDIBIS_SESSION_HOURS` | `12` |
| bootstrap user | `REDIBIS_ADMIN_USER` | — |
| bootstrap password | `REDIBIS_ADMIN_PASSWORD` | — |
| force Secure cookies | `REDIBIS_FORCE_SECURE_COOKIES` | `0` |
| HTTPS redirect in-app | `REDIBIS_REQUIRE_HTTPS` | `0` |
| CORS origins | `REDIBIS_CORS_ORIGINS` | empty (same-origin) |

`backend: db` with an empty `db_url` **fails startup**. There is no silent
JSON fallback.

The JSON backend (`auth.backend: json`) is for a **single dashboard process**.
It caches users and sessions until the file mtime changes. Two replicas sharing
the same `users.json` / `sessions.json` would each cache independently and could
miss a peer's logout. Multi-replica deployments must set `auth.backend: db`.

Expired sessions are dropped on the next session write (login, logout, or
presenting an already-expired token). They are not swept by a background TTL
thread.

Password hashes live next to `store_path`, not in the contracts bucket. If
`configs/auth/users.json` is missing, a leftover `{contracts-bucket}/_meta/users.json`
is **ignored** (a warning is logged) and the empty-store default
`admin` / `admin` is created in `store_path`. Copy the leftover file to
`auth.store_path` only if you still need those old accounts.

---

## First start (empty `users.json`)

On first boot with **no users file** at `auth.store_path` (default
`configs/auth/users.json`):

1. If `REDIBIS_ADMIN_USER` / `REDIBIS_ADMIN_PASSWORD` are set, that admin is created
   (`REDIBIS_ADMIN_PASSWORD` must be at least 12 characters).
2. Otherwise Redibis creates **`admin` / `admin`**. The server log prints a
   DEFAULT LOGIN banner. Change it immediately at `/users`.

Bootstrap is inert once any admin exists — it cannot reset a password. A later
start logs `admin already exists … password is not printed again` and the path
of `users.json`. To restore the empty-store default:

```bash
./scripts/webapp.sh --restart --clean-users --background
```

That deletes `configs/auth/users.json` (and leftover `_meta/users.json`) then
starts the dashboard; login is **`admin` / `admin`**.

New passwords chosen at `/users` must be at least 12 characters. Legacy unsalted
SHA-256 hashes still log in and are upgraded to PBKDF2 on the next successful
login.

While `admin` / `admin` still verifies, a warning is logged every 60 seconds
until the password is changed. The account is not auto-disabled.

`auth.enabled: false` is local development only and logs the same repeating
warning.

If first-boot admin creation fails (disk or object-store I/O), the process
still starts but logs **`auth bootstrap FAILED — no admin may exist`** at
ERROR. The 60-second warning also reports
`auth: no admin account exists — nobody can sign in as operator`. Check
that log before assuming login is broken.

---

## Calling the API (curl)

Auth is **on by default**. Unauthenticated `/api/*` calls return
`{"detail":"authentication required"}` (HTTP **401**). There is no API key.
Sign in once, keep the cookie jar, and send the CSRF header on every
POST/PUT/PATCH/DELETE.

**CSRF rule:** mutating requests must send `X-CSRF-Token`. Body-carried CSRF
is only supported on `/login` and `/logout`, which verify in-handler. A
missing header on any other POST/PUT/PATCH/DELETE returns **403**
`CSRF header required (X-CSRF-Token)`. Do not add a new HTML
`<form method="post">` outside those two paths unless it is submitted by
JavaScript that sets the header (the dashboard is JS-driven today).

Public without a session: `GET /health`, `GET /healthz`, `GET|POST /login`,
`POST /logout`, `/static/*`, and share-link paths. **Not** public: `/ready`,
`/docs` (Swagger), and every `/api/*` route.

The CLI does not use dashboard sessions — see [Authorization boundary](#authorization-boundary).

### Cookie jar (once per shell)

```bash
export RB_BASE="${RB_BASE:-http://127.0.0.1:8000}"
export RB_COOKIES="${RB_COOKIES:-/tmp/redibis.cookies}"
export RB_USER="${RB_USER:-admin}"
# Empty users.json defaults to admin/admin; set RB_PASSWORD if you changed it.
export RB_PASSWORD="${RB_PASSWORD:-admin}"

curl -s -c "$RB_COOKIES" -b "$RB_COOKIES" "$RB_BASE/login" -o /dev/null
CSRF=$(awk '$6 == "redibis_csrf" { print $7 }' "$RB_COOKIES" | tail -1)

curl -s -c "$RB_COOKIES" -b "$RB_COOKIES" -X POST "$RB_BASE/login" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "X-CSRF-Token: $CSRF" \
  -d "{\"username\":\"$RB_USER\",\"password\":\"$RB_PASSWORD\"}"

CSRF=$(awk '$6 == "redibis_csrf" { print $7 }' "$RB_COOKIES" | tail -1)
```

A successful login returns `{"status":"ok","user":{...}}`. After that:

| Kind | Extra curl flags |
|------|------------------|
| GET | `-b "$RB_COOKIES"` |
| POST / PUT / PATCH / DELETE | `-b "$RB_COOKIES" -H "X-CSRF-Token: $CSRF"` |

Example:

```bash
curl -s -b "$RB_COOKIES" "$RB_BASE/api/me"

curl -s -b "$RB_COOKIES" -X POST "$RB_BASE/api/pii/text/scan" \
  -H "Content-Type: application/json" \
  -H "X-CSRF-Token: $CSRF" \
  -d '{"text":"Hi, this is Ahmed Hassan. My mobile is 01012345678.","language":"en"}'
```

POST without a session → **401**. POST with a session but no `X-CSRF-Token`
header → **403** `CSRF header required (X-CSRF-Token)` (login/logout forms
that omit the body field still return `CSRF token missing or invalid`).
The **explorer** role is read-only for contract and settings writes.
Explorers **may** `POST /api/gateway/*` (scan / suggest-policy / deidentify)
because those routes compute in memory and store nothing. Raw
`POST /api/pii/text/*` stays **admin-only**.

### Python (`requests`)

```python
import os
import requests

base = os.environ.get("RB_BASE", "http://127.0.0.1:8000")
session = requests.Session()
session.get(f"{base}/login")
csrf = session.cookies["redibis_csrf"]
session.post(
    f"{base}/login",
    json={"username": os.environ.get("RB_USER", "admin"),
          "password": os.environ["RB_PASSWORD"]},
    headers={"X-CSRF-Token": csrf, "Accept": "application/json"},
    timeout=30,
).raise_for_status()
csrf = session.cookies["redibis_csrf"]

def api(method: str, path: str, **kwargs):
    headers = dict(kwargs.pop("headers", None) or {})
    headers["X-CSRF-Token"] = csrf
    return session.request(method, f"{base}{path}", headers=headers, timeout=60, **kwargs)
```

---

## HTTPS

### Path A — reverse proxy (recommended for CDP)

TLS terminates at nginx or Caddy. Bind uvicorn to loopback:

```bash
uvicorn redibis.webapp.backend:app --host 127.0.0.1 --port 8000 \
    --proxy-headers --forwarded-allow-ips="127.0.0.1"
```

Both flags are required. Without them uvicorn ignores `X-Forwarded-Proto`, the
app thinks every request is HTTP, and the session cookie is never `Secure`.

```nginx
server {
    listen 443 ssl http2;
    server_name redibis.example.com;

    ssl_certificate     /etc/ssl/certs/redibis.crt;
    ssl_certificate_key /etc/ssl/private/redibis.key;
    ssl_protocols       TLSv1.2 TLSv1.3;

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}

server {
    listen 80;
    server_name redibis.example.com;
    return 301 https://$host$request_uri;
}
```

Set `REDIBIS_FORCE_SECURE_COOKIES=1` if the proxy cannot send `X-Forwarded-Proto`.

`REDIBIS_REQUIRE_HTTPS` (in-app HTTP→HTTPS redirect) defaults **off**. Enable it
only after `--proxy-headers` is verified. A proxy that terminates TLS but does
not forward `X-Forwarded-Proto` plus this flag is an infinite redirect loop.
Prefer letting the proxy redirect, as above.

### Path B — uvicorn serves TLS directly

```bash
uvicorn redibis.webapp.backend:app --host 0.0.0.0 --port 8443 \
    --ssl-keyfile /etc/ssl/private/redibis.key \
    --ssl-certfile /etc/ssl/certs/redibis.crt
```

There is no HTTP listener, so users must type `https://`. Certificate renewal
is manual.

The session cookie is `Secure` only on HTTPS (or when
`REDIBIS_FORCE_SECURE_COOKIES=1`). Hardcoding `Secure` breaks local HTTP: the
login form accepts the password and bounces straight back to `/login` because
the browser discards the cookie.

HSTS is sent on HTTPS responses only.

### CORS

`REDIBIS_CORS_ORIGINS` defaults empty (same-origin). When it is set, CORS
middleware wraps authentication so a cross-origin caller still sees
`Access-Control-Allow-Origin` on **401** / **403**, not an opaque network
error. Leave it empty unless a browser on another origin must call the API.

### Air-gapped certificates

No Let's Encrypt without egress. In order of preference:

1. **Corporate/internal CA** — clients already trust the root.
2. **Self-signed with a documented fingerprint** — distribute the cert; do not
   train users to click through TLS warnings.
3. `openssl req` with `subjectAltName` (a cert without SAN is rejected by
   current browsers):

```bash
openssl req -x509 -newkey rsa:2048 -sha256 -days 365 -nodes \
  -keyout /etc/ssl/private/redibis.key \
  -out /etc/ssl/certs/redibis.crt \
  -subj "/CN=redibis.example.com" \
  -addext "subjectAltName=DNS:redibis.example.com,DNS:localhost,IP:127.0.0.1"
```

---

## Health

`GET /health` and `GET /healthz` are unauthenticated **liveness** probes
(`{"status":"ok"}`). They do not return version, config, or store detail.
Do not use `/docs` as a liveness check — Swagger requires a session.

`GET /ready` remains a readiness probe and **requires a session** when auth is
on. Point load-balancer liveness at `/health`. Cookie-jar login for `/ready`:
[Calling the API](#calling-the-api-curl).

---

## Share links

`/share/{token}` and `/api/share/{token}` stay account-free. The token is the
credential; `apply_scoped_edits` still enforces scope. Failed token lookups are
rate-limited per IP.
