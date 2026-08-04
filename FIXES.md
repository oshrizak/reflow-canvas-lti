# Fix-it list — reflow-canvas-lti

Audit date: 2026-08-03. Against commit `8c53305`.
Method: static read of `connector/`, full test suite on Python 3.12 (75 passed
before this branch, 80 after), plus a line-ending check of the working tree.

Ordered: correctness and security first, multi-tenancy second.

---

## 0. Already fixed upstream — do not re-investigate

An earlier audit ran against the `Desktop\Reflow Canvas API\phase2_*` snapshot,
which is well behind this repo. Three findings from it are **already resolved
here**. Recording them so nobody burns time re-finding them:

| Stale finding | Status in `8c53305` |
|---|---|
| `course_id` used the opaque LTI `context.id` | **Fixed.** `lti/jwt_validation.py:104` prefers the `custom.course_id` claim, and `lti/routes.py:375` declares `"course_id": "$Canvas.course.id"` in `custom_fields`. Falls back to `context.id` only if the platform doesn't expand the substitution. |
| `deployment_id` only validated when a stored list was non-empty | **Fixed.** `lti/jwt_validation.py:147` compares unconditionally and raises `LtiValidationError` on mismatch. |
| JWKS fetched with blocking `urllib` inside async handlers | **Fixed.** `lti/jwt_validation.py:49` uses `httpx.AsyncClient`. |

---

## 1. Done in this branch

### 1.1 — Line endings: 660-line phantom diffs  *(severity: hygiene, but blocks review)*

`connector/api/canvas_oauth.py` and `connector/utils/retry_helpers.py` were
CRLF in the working tree and LF in the index. `git diff` showed 660 insertions
and 660 deletions; `git diff --ignore-all-space` showed **nothing**. Committing
that through GitHub Desktop would have buried every real change.

Cause: no `.gitattributes`, `core.autocrlf` unset, editing on Windows.

**Fix applied:** added `.gitattributes` normalising everything to LF, with
`*.ps1` held at CRLF (PowerShell 5.1 on EBW-WCA01 mis-parses LF-only files —
see the server runbook). Adding the file alone cleared both phantom diffs.

**Verify:** `git status --porcelain` shows neither file as modified.

### 1.2 — `revoked_at` was never enforced  *(severity: high — security)*

`mark_revoked()` (`lti/platform_store.py:195`) set the marker, and
`put_platform()` carefully preserved it across relaunches
(`platform_store.py:76`). Nothing ever read it. `grep -c revoked
connector/lti/routes.py` returned **0** — a revoked platform kept launching
normally, so revocation was a no-op.

**Fix applied:** `lti/routes.py` now captures the merged record returned by
`put_platform()` and rejects the launch with 403 when `revoked_at` is set. The
gate sits **outside** the surrounding `try/except`, which deliberately swallows
every exception and would otherwise eat the `HTTPException`.

**Tests:** `tests/unit/test_platform_revocation.py` (5 tests). The important one
is `test_relaunch_does_not_lift_revocation` — every launch builds a fresh
`PlatformInstall` with `revoked_at=None`, so if the merge ever dropped the
marker, revocation would silently lift itself.

---

## 2. Correctness / security — not yet done

### 2.1 — 39 of 41 `tk()` callsites are not tenant-scoped  *(severity: high)*

`canvas/tenant.py:79` provides `tk(suffix, *, platform=None)`, which sandboxes a
Redis key under `{prefix}:p:{platform_id}:` when a platform is passed. Its
docstring is candid: *"Phase 7 ... does NOT yet retrofit every callsite."*

Measured: `grep -rn "tk(" connector --include=*.py` → **41 calls, 2 with
`platform=`.** The other 39 write at deployment level, so two Canvas instances
served by one deployment share those keys.

**This is a data migration, not just a code change.** Renaming keys orphans the
live CSUEB pilot's data. Sequence it:

1. Inventory the 39 callsites; classify each as genuinely shared (deploy
   metadata, dead-letter queues) or per-tenant (jobs, tokens, scored files,
   course→owner maps).
2. For per-tenant keys, dual-read: try the tenant-scoped key, fall back to the
   legacy key, and write both for one release.
3. Backfill existing keys under the scoped namespace.
4. Drop the fallback and the legacy writes in a later release.

Until this lands, **one deployment per Canvas instance** is the only safe
configuration. That is fine today (CSUEB only) but is the thing to fix before a
second campus shares an instance.

### 2.2 — No test coverage of the LTI launch path  *(severity: medium)*

80 tests, none of which exercise `/lti/login` or `/lti/launch`. There's no
harness for signing an `id_token`, mocking the platform JWKS, and driving the
OIDC round trip, so the highest-risk code in the connector is untested — the
revocation gate in 1.2 included.

**Fix:** add a launch fixture — generate a throwaway RSA keypair, serve it as a
JWKS via `respx`, mint a signed `id_token` with the required claims, and drive
`login → launch`. Once that exists, the gate gets a real end-to-end test and
regressions in claim handling become catchable.

---

## 3. Multi-tenancy — what actually blocks a second campus

### 3.1 — Canvas OAuth API credentials are global  *(severity: high — this is the blocker)*

`config.py:163` and `:171` define `canvas_oauth_client_id` /
`canvas_oauth_client_secret` as single deployment-wide settings.
`canvas/user_oauth.py:181`, `:224` and `:273` read them from global settings:

```python
oauth_cid = (getattr(_s, "canvas_oauth_client_id", "") or "").strip()
effective_client_id = oauth_cid or platform.client_id
```

`PlatformInstall` (`lti/platform.py:62`) has **no fields** for OAuth API
credentials — it carries `issuer`, `client_id`, `deployment_id`, the three
endpoint URLs, `canvas_api_base`, `canvas_domain`, `label`, `granted_scopes`,
timestamps and `revoked_at`.

Canvas Cloud rejects `authorization_code` grants against an LTI 1.3 key, so a
separate non-LTI API Key with a `client_secret` is required per institution.
There is nowhere to put a second institution's key. **A second campus cannot
bring their own credentials today** — they need their own deployment.

**Fix:** add `oauth_client_id` and an encrypted `oauth_client_secret` to
`PlatformInstall`; have `user_oauth.py` prefer the per-platform value and fall
back to the global setting for backward compatibility. Secrets must be
encrypted at rest — Redis holds them and there's no envelope encryption today.

### 3.2 — One LTI keypair per deployment  *(severity: medium)*

`lti/keys.py:75` `load_private_key()` reads a single path from config, so the
tool has exactly one identity. Running **multiple distinct LTI tools** from one
instance is not possible: each tool needs its own keypair, `kid`, and tool
config. Different *campuses* using the *same* tool is fine — that's what
`PlatformInstall` handles.

**Fix (only if you actually want multiple tools):** key the private key by tool
id, serve a multi-key JWKS, and select the signing key per platform. Non-trivial;
skip unless it's a real requirement.

### 3.3 — No control plane  *(severity: medium)*

Routers are only `/lti`, `/canvas/consent`, `/canvas/oauth`, `/canvas/panorama`,
`/canvas/review`. There is no admin surface. Platforms self-register implicitly
by upsert-on-launch (`lti/routes.py:216`) with no authentication on who may
register — anyone who points a Canvas Developer Key at the public URL becomes a
tenant, and the only way to undo it is `mark_revoked()` from a shell.

**Fix:** a small admin API (list / inspect / label / revoke / restore platforms,
plus per-tenant credential entry once 3.1 lands). Auth for it is deliberately
left open here — it holds Canvas client secrets, so it needs a higher bar than
the viewer's basic auth.

---

## 4. Presentation accuracy

Slide 16 of the ATI Summit deck says storage is *"namespaced by platform_id."*
The mechanism exists — `compute_platform_id(issuer, client_id, deployment_id)`
and `tk(platform=...)` — but only 2 of 41 callsites use it (see 2.1), and the
per-campus OAuth key claim on the same slide is not implemented (see 3.1).

Slide 20 lists "Tenant admin portal + per-campus billing" under H1 2027, which
is the honest framing. Recommend softening slide 16 to match: the LTI 1.3
registration layer is genuinely multi-tenant today; per-tenant credentials and
full key namespacing are in progress.
