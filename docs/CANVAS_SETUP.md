# Canvas setup

This walks a Canvas admin through wiring the connector up to a Canvas instance.
You need:

- Admin access to your Canvas account.
- The connector running and reachable over HTTPS (for production) or via
  a tunnel like Cloudflared / ngrok (for dev). Plain HTTP works for
  same-host testing only.
- The connector's `LTI_PUBLIC_URL` set to the public origin Canvas will reach
  it on.

You'll create **two** Canvas Developer Keys:

1. An **LTI Developer Key** that powers the launch + LTI Advantage scopes.
2. An **API Developer Key** that powers the per-instructor OAuth-2 flow.

Canvas Cloud doesn't accept LTI Advantage tokens against `/api/v1/*` REST
calls, so the second key is mandatory there.

## 1 · LTI Developer Key

From Canvas Admin → Developer Keys → "+ Developer Key" → **LTI Key**, choose
**Paste JSON** and paste what the connector returns at:

```
GET {LTI_PUBLIC_URL}/lti/config.json
```

The connector populates every field automatically (title, scopes, public JWK
URL, placements, custom fields). Save the key, then turn its state to **ON**.

Canvas will display a **Client ID** like `190000000000123`. Copy it into the
connector's `.env`:

```
LTI_CLIENT_ID=190000000000123
LTI_ISSUER=https://canvas.instructure.com         # or your instance host
LTI_DEPLOYMENT_ID=<get from the install step>
LTI_AUTH_LOGIN_URL=https://canvas.instructure.com/api/lti/authorize_redirect
LTI_AUTH_TOKEN_URL=https://canvas.instructure.com/login/oauth2/token
LTI_JWKS_URL=https://canvas.instructure.com/api/lti/security/jwks
```

If you self-host Canvas, swap the host in the three URLs above. For Canvas
Cloud's beta/test environments, swap `canvas.instructure.com` for
`canvas.beta.instructure.com` or `canvas.test.instructure.com`.

### Install the tool in an account or course

After the key is saved, install it in the account or sub-account that should
see it. Canvas will assign a numeric `deployment_id` — capture it. The
connector validates this on every launch.

### Scopes

The `/lti/config.json` scopes list is pinned to what the watcher and bridge
actually need:

- Canvas data API (file listings, page CRUD, conversations, etc.).
- LTI Advantage (NRPS contextmembership, AGS lineitem).

When an admin approves the LTI key they approve all listed scopes together;
no per-call consent screen.

## 2 · API Developer Key (per-instructor OAuth)

Canvas requires a **separate, non-LTI** key for the `/login/oauth2/auth` flow.
The watcher and bridge use the resulting per-instructor token for everything
under `/api/v1/*`.

From Canvas Admin → Developer Keys → "+ Developer Key" → **API Key**:

- **Key Name:** Equalify Reflow (or similar).
- **Redirect URIs:** `{LTI_PUBLIC_URL}/canvas/oauth/callback`
- **Vendor Code:** any short identifier.
- **Icon URL:** optional.
- **Enforce Scopes:** ON. Toggle on the same Canvas data API scopes you
  approved on the LTI key.

Save and **ON** the key. Capture:

```
CANVAS_OAUTH_CLIENT_ID=<numeric client id>
CANVAS_OAUTH_CLIENT_SECRET=<the secret Canvas shows once>
```

The secret is shown only at creation — store it in your secret manager.

## 3 · Wire `.env` and restart

```bash
LTI_ENABLED=true
LTI_PUBLIC_URL=https://reflow-canvas-lti.your-org.edu
LTI_CLIENT_ID=...
LTI_DEPLOYMENT_ID=...
LTI_ISSUER=https://canvas.instructure.com
LTI_AUTH_LOGIN_URL=https://canvas.instructure.com/api/lti/authorize_redirect
LTI_AUTH_TOKEN_URL=https://canvas.instructure.com/login/oauth2/token
LTI_JWKS_URL=https://canvas.instructure.com/api/lti/security/jwks

CANVAS_API_URL=https://canvas.instructure.com
CANVAS_OAUTH_CLIENT_ID=...
CANVAS_OAUTH_CLIENT_SECRET=...
CANVAS_WATCHED_COURSES=                 # comma-separated, or leave blank in multi-tenant mode
CANVAS_ALLOWED_ORIGINS=https://canvas.instructure.com
MULTI_TENANT_WATCHER=false              # set true once multiple platforms launch the tool
```

Restart the connector. Visit `{LTI_PUBLIC_URL}/lti/healthz` and confirm it
says `enabled: yes`.

## 4 · First launch

In a course (or the account), open the **course navigation** tool list and
add **Accessible Documents** (or whatever you titled the LTI key). Faculty
click it once to complete the OAuth consent flow — after that the connector
has a stored access + refresh token and can scan the course's files in the
background.

## 5 · Theme-Editor overlay (optional but recommended)

The panorama overlay is what surfaces dial badges and "Convert" buttons in
Canvas's native file UI. Admin → Themes → edit the active theme → Append
the following inline:

```html
<script src="{LTI_PUBLIC_URL}/panorama.js" defer></script>
```

The bundle is permission-aware: students see read-only dials, instructors see
review actions. Cache-Control is `no-cache` so updates ship without a theme
re-publish.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `/lti/config.json` returns 404 | `LTI_ENABLED=false` |
| `/lti/jwks` returns 500 | Keypair missing — run `./scripts/generate_lti_keys.sh` |
| `Unknown issuer` on launch | `LTI_ISSUER` doesn't match what Canvas sent |
| `Unexpected audience` on launch | `LTI_CLIENT_ID` doesn't match the LTI Developer Key |
| `Unexpected deployment_id` | `LTI_DEPLOYMENT_ID` not set or wrong placement |
| Canvas REST calls 401 after consent | API key scopes don't cover the call — re-toggle Enforce Scopes on the API Key |
| Watcher logs "no watched courses" | `CANVAS_WATCHED_COURSES` empty AND `MULTI_TENANT_WATCHER=false` |
