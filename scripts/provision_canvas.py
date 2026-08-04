#!/usr/bin/env python3
"""Provision this tool's LTI 1.3 registration in Canvas, end to end.

Replaces a fiddly manual sequence across two Canvas admin screens — create an
LTI Developer Key from the tool's config URL, enable it, deploy it to a course,
then read back the ``deployment_id`` — which is exactly the sequence that was
never completed on the CSUEB box (``LTI_CLIENT_ID=FILL-IN-PHASE-4``).

Steps
-----
1.  ``POST /api/lti/accounts/:account_id/developer_keys/tool_configuration``
    with ``tool_configuration[settings_url]`` pointing at the tool's own
    ``/lti/config.json``, so Canvas fetches the canonical config rather than
    anyone hand-copying JSON.
2.  ``PUT /api/v1/developer_keys/:id`` to set workflow_state ``on``. A key
    that exists but is off produces launches that fail with no useful error.
3.  ``POST /api/v1/courses/:course_id/external_tools`` with ``client_id``,
    which mints the deployment.
4.  Read ``deployment_id`` back off the created tool.
5.  Print the exact ``.env`` lines to paste.

Not covered
-----------
The second, non-LTI **API Key** used for the per-instructor OAuth flow
(``CANVAS_OAUTH_CLIENT_ID`` / ``_SECRET``). Canvas has no API for minting one
and its secret is shown exactly once in the UI, so that step stays manual.
The script tells you where to go.

It also never writes to ``.env``. It prints; you paste.

Usage
-----
    export CANVAS_BASE_URL="https://school.instructure.com"
    export CANVAS_ADMIN_TOKEN="<admin token>"
    export CANVAS_ACCOUNT_ID="1"
    export CANVAS_COURSE_ID="50594"
    export TOOL_BASE_URL="https://reflow.example.edu"

    python scripts/provision_canvas.py                       # dry run
    python scripts/provision_canvas.py --apply               # do it
    python scripts/provision_canvas.py --apply \
        --client-id 211450000000000383                       # resume

``--client-id`` matters after a partial failure: step 1 succeeds by creating a
real key, so a naive re-run leaves a duplicate behind. Pass the id the failed
run printed and it resumes from step 2.

Needs an account-admin token with ``manage_developer_keys``. Revoke it when
you're done — it is far more privileged than anything the connector needs at
runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx


def _env(name: str) -> str:
    val = (os.environ.get(name) or "").strip()
    if not val:
        sys.exit(f"Missing required environment variable: {name}")
    return val


def _check(resp: httpx.Response, what: str) -> dict:
    if resp.is_error:
        body = resp.text[:500]
        hint = ""
        if resp.status_code == 401:
            hint = "\nHint: token rejected. Is it an admin token for this account?"
        elif resp.status_code == 403:
            hint = (
                "\nHint: authenticated but not permitted. Developer-key work "
                "needs manage_developer_keys at the root account."
            )
        elif resp.status_code == 404:
            hint = (
                "\nHint: check CANVAS_ACCOUNT_ID (root is usually 1) and that "
                "CANVAS_BASE_URL is your institutional host."
            )
        sys.exit(f"\n{what} failed: HTTP {resp.status_code}\n{body}{hint}")
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        sys.exit(f"{what}: expected JSON, got:\n{resp.text[:300]}")


def _fetch_tool_config(config_url: str) -> dict:
    """Fetch and sanity-check the tool's own config. Canvas fetches this URL
    itself, so anything we can't read, Canvas can't either."""
    try:
        resp = httpx.get(config_url, timeout=30.0)
    except httpx.HTTPError as exc:
        sys.exit(f"Could not reach {config_url}: {exc}")
    if resp.is_error:
        sys.exit(f"{config_url} returned HTTP {resp.status_code}")
    try:
        cfg = resp.json()
    except ValueError:
        sys.exit(f"{config_url} did not return JSON")

    missing = [
        k for k in ("title", "oidc_initiation_url", "target_link_uri") if not cfg.get(k)
    ]
    if missing:
        sys.exit(f"Tool config is missing required keys: {missing}")
    if not (cfg.get("public_jwk") or cfg.get("public_jwk_url")):
        sys.exit("Tool config has neither public_jwk nor public_jwk_url")
    for ext in cfg.get("extensions") or []:
        if not ext.get("privacy_level"):
            print(
                "      WARNING: extensions[].privacy_level is absent. Canvas "
                "defaults to 'anonymous', which drops the user's name and "
                "email from the launch and stops $Canvas.user.id expanding."
            )
    return cfg


def _create_key(
    client: httpx.Client, base: str, account: str, config_url: str,
    cfg: dict, tool_name: str,
) -> str:
    """Create the developer key + tool configuration. Returns the client_id."""
    created = _check(
        client.post(
            f"{base}/api/lti/accounts/{account}/developer_keys/tool_configuration",
            # Canvas namespaces this payload: settings_url lives under
            # tool_configuration, not at the top level. Flat returns
            # 400 "tool_configuration is missing", which reads like a
            # permission problem but isn't.
            json={
                "tool_configuration": {"settings_url": config_url},
                "developer_key": {
                    "name": tool_name,
                    # Canvas stores these as a newline-delimited text field.
                    # All of them must be listed or the post-login redirect
                    # fails with "Invalid redirect_uri".
                    "redirect_uris": "\n".join(
                        cfg.get("redirect_uris") or [cfg["target_link_uri"]]
                    ),
                },
            },
        ),
        "Create developer key",
    )
    dev_key = created.get("developer_key") or {}
    client_id = str(
        dev_key.get("id")
        or created.get("developer_key_id")
        or (created.get("tool_configuration") or {}).get("developer_key_id")
        or ""
    )
    if not client_id:
        print(json.dumps(created, indent=2)[:1200])
        sys.exit("Could not determine the client_id from the response above.")
    return client_id


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="perform the writes")
    ap.add_argument(
        "--tool-name",
        default="Equalify Reflow - Accessible Documents",
        help="developer key name shown in the Canvas admin list",
    )
    ap.add_argument(
        "--client-id",
        default="",
        help="resume with an existing key instead of creating a duplicate",
    )
    ap.add_argument(
        "--install-at",
        choices=("course", "account"),
        default="course",
        help=(
            "where to deploy. Some institutions block course-level installs "
            "by client ID ('This app has been locked by an administrator'); "
            "account-level often still works, and for a campus-wide tool is "
            "arguably the right home anyway."
        ),
    )
    args = ap.parse_args()

    base = _env("CANVAS_BASE_URL").rstrip("/")
    token = _env("CANVAS_ADMIN_TOKEN")
    account = _env("CANVAS_ACCOUNT_ID")
    course = _env("CANVAS_COURSE_ID")
    tool = _env("TOOL_BASE_URL").rstrip("/")
    config_url = f"{tool}/lti/config.json"

    print("Plan")
    print(f"  Canvas          {base}")
    print(f"  Account         {account}")
    print(f"  Course          {course}")
    print(f"  Tool config     {config_url}")
    if args.client_id:
        print(f"  Resuming with   client_id {args.client_id}")
    print()

    print("[0/4] Checking the tool's config endpoint is reachable...")
    cfg = _fetch_tool_config(config_url)
    print(f"      OK — '{cfg.get('title')}'")

    if not args.apply:
        print()
        print("Dry run. Re-run with --apply to make these changes:")
        if not args.client_id:
            print(f"  1. create an LTI developer key in account {account}")
        print("  2. turn that key ON")
        print(f"  3. deploy it to course {course}")
        print("  4. read back the deployment_id")
        return 0

    with httpx.Client(
        timeout=60.0, headers={"Authorization": f"Bearer {token}"}
    ) as client:
        if args.client_id:
            client_id = args.client_id.strip()
            print("[1/4] Skipped — resuming with existing key")
        else:
            print("[1/4] Creating the LTI developer key...")
            client_id = _create_key(
                client, base, account, config_url, cfg, args.tool_name
            )
        print(f"      client_id = {client_id}")

        # Update/delete on a developer key are NOT account-scoped in Canvas —
        # it's PUT /api/v1/developer_keys/:id. The account-scoped path returns
        # Canvas's HTML 404 page, which looks like a wrong account id but isn't.
        print("[2/4] Enabling the key...")
        _check(
            client.put(
                f"{base}/api/v1/developer_keys/{client_id}",
                json={"developer_key": {"workflow_state": "on"}},
            ),
            "Enable developer key",
        )
        print("      key state on")

        # Canvas tracks TWO switches. The key's own workflow_state above, and
        # a per-account binding. The admin UI's ON toggle sets both; the API
        # does not. With the binding unset, installing by client_id fails with
        # "This app has been locked by an administrator and cannot be installed
        # via client ID" — which sounds like a policy decision someone made,
        # but is just the binding sitting at its default.
        _check(
            client.post(
                f"{base}/api/v1/accounts/{account}/developer_keys/"
                f"{client_id}/developer_key_account_bindings",
                json={"developer_key_account_binding": {"workflow_state": "on"}},
            ),
            "Bind developer key to account",
        )
        print("      account binding on")

        if args.install_at == "account":
            scope = f"accounts/{account}"
            where = f"account {account}"
        else:
            scope = f"courses/{course}"
            where = f"course {course}"

        print(f"[3/4] Deploying to {where}...")
        tool_obj = _check(
            client.post(
                f"{base}/api/v1/{scope}/external_tools",
                json={"client_id": client_id},
            ),
            "Install external tool",
        )
        tool_id = tool_obj.get("id")
        print(f"      external tool id = {tool_id}")

        print("[4/4] Reading deployment_id...")
        deployment_id = str(tool_obj.get("deployment_id") or "")
        if not deployment_id and tool_id:
            detail = _check(
                client.get(f"{base}/api/v1/{scope}/external_tools/{tool_id}"),
                "Fetch external tool",
            )
            deployment_id = str(detail.get("deployment_id") or "")
        if not deployment_id:
            print(
                "      Canvas did not return deployment_id.\n"
                f"      Read it manually: {base}/courses/{course}/settings/configurations"
                " -> gear next to the app -> Deployment Id"
            )

    print()
    print("=" * 68)
    print("Add these to /opt/reflow/reflow-canvas-lti/.env")
    print("=" * 68)
    print(f"LTI_CLIENT_ID={client_id}")
    print(f"LTI_DEPLOYMENT_ID={deployment_id or '<read from the Canvas UI, see above>'}")
    print("MULTI_TENANT_WATCHER=true")
    print("CANVAS_WATCHED_COURSES=")
    print()
    print("Still manual — Canvas has no API to mint an API Key, and its secret")
    print("is shown only once:")
    print(f"  {base}/accounts/{account}/developer_keys")
    print("  + Developer Key -> + API Key")
    print(f"  Redirect URI: {tool}/canvas/oauth/callback")
    print("  Then set CANVAS_OAUTH_CLIENT_ID and CANVAS_OAUTH_CLIENT_SECRET.")
    print()
    print("Then: sudo docker compose restart connector, launch the tool from")
    print(f"course {course}, and look for 'platform upsert' in the logs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
