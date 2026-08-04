#!/usr/bin/env python3
"""Provision this tool's LTI 1.3 registration in Canvas, end to end.

Replaces a fiddly manual sequence across two Canvas admin screens — create an
LTI Developer Key from the tool's config URL, enable it, deploy it to a course,
then read back the ``deployment_id`` — which is exactly the sequence that was
never completed on the CSUEB box (``LTI_CLIENT_ID=FILL-IN-PHASE-4``).

What it does
------------
1.  ``POST /api/lti/accounts/:account_id/developer_keys/tool_configuration``
    with ``settings_url`` pointing at the tool's own ``/lti/config.json``, so
    Canvas fetches the canonical config rather than anyone hand-copying JSON.
2.  ``PUT /api/v1/accounts/:account_id/developer_keys/:id`` to set the key
    workflow_state to ``on``. A key that is created but left off produces a
    launch that fails with no useful error.
3.  ``POST /api/v1/courses/:course_id/external_tools`` with ``client_id``,
    which creates the deployment.
4.  Reads ``deployment_id`` back off the created tool.
5.  Prints the exact ``.env`` lines to paste.

What it deliberately does NOT do
--------------------------------
*   It does not create the second, non-LTI **API Key** used for the
    per-instructor OAuth flow (``CANVAS_OAUTH_CLIENT_ID`` / ``_SECRET``).
    Canvas has no API for minting one, and its secret is shown exactly once
    in the UI. That step stays manual — the script tells you when you're there.
*   It does not write to ``.env``. It prints; you paste. A script that edits
    a live production ``.env`` unattended is a bad idea, and this one is meant
    to be run against a real institution's Canvas.

Usage
-----
    export CANVAS_BASE_URL="https://csueb.instructure.com"
    export CANVAS_ADMIN_TOKEN="<admin token>"
    export CANVAS_ACCOUNT_ID="1"              # root account, usually 1
    export CANVAS_COURSE_ID="50594"
    export TOOL_BASE_URL="https://accessibility-checker.csueastbay.edu"

    python scripts/provision_canvas.py            # show what it would do
    python scripts/provision_canvas.py --apply    # actually do it

Requires an account-admin token (``manage_developer_keys``). Generate one at
/profile/settings, and revoke it when you're finished — it is far more
privileged than anything the connector itself needs at runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx


def _env(name: str, *, required: bool = True) -> str:
    val = (os.environ.get(name) or "").strip()
    if required and not val:
        sys.exit(f"Missing required environment variable: {name}")
    return val


def _check(resp: httpx.Response, what: str) -> dict:
    if resp.is_error:
        body = resp.text[:600]
        hint = ""
        if resp.status_code == 401:
            hint = "\nHint: token rejected. Is it an *admin* token for this account?"
        elif resp.status_code == 403:
            hint = (
                "\nHint: the token authenticated but lacks permission. "
                "Developer-key work needs the manage_developer_keys right at "
                "the root account."
            )
        elif resp.status_code == 404:
            hint = (
                "\nHint: check CANVAS_ACCOUNT_ID (root is usually 1) and that "
                "CANVAS_BASE_URL is your institutional host, not "
                "canvas.instructure.com."
            )
        sys.exit(f"\n{what} failed: HTTP {resp.status_code}\n{body}{hint}")
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        sys.exit(f"{what}: expected JSON, got:\n{resp.text[:400]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--apply",
        action="store_true",
        help="perform the writes; without it the script only reports the plan",
    )
    ap.add_argument(
        "--tool-name",
        default="Equalify Reflow - Accessible Documents",
        help="developer key name shown in the Canvas admin list",
    )
    args = ap.parse_args()

    base = _env("CANVAS_BASE_URL").rstrip("/")
    token = _env("CANVAS_ADMIN_TOKEN")
    account = _env("CANVAS_ACCOUNT_ID")
    course = _env("CANVAS_COURSE_ID")
    tool = _env("TOOL_BASE_URL").rstrip("/")
    config_url = f"{tool}/lti/config.json"

    headers = {"Authorization": f"Bearer {token}"}

    print("Plan")
    print(f"  Canvas          {base}")
    print(f"  Account         {account}")
    print(f"  Course          {course}")
    print(f"  Tool config     {config_url}")
    print()

    with httpx.Client(timeout=60.0, headers=headers) as client:
        # Sanity: is the tool config actually reachable and shaped right?
        # Canvas will fetch this URL itself, so if we can't, Canvas can't.
        print("[0/4] Checking the tool's config endpoint is reachable...")
        try:
            cfg = httpx.get(config_url, timeout=30.0)
        except httpx.HTTPError as exc:
            sys.exit(f"Could not reach {config_url}: {exc}")
        if cfg.is_error:
            sys.exit(f"{config_url} returned HTTP {cfg.status_code}")
        try:
            cfg_json = cfg.json()
        except ValueError:
            sys.exit(f"{config_url} did not return JSON")

        missing = [k for k in ("title", "oidc_initiation_url", "target_link_uri") if not cfg_json.get(k)]
        if missing:
            sys.exit(f"Tool config is missing required keys: {missing}")
        if not (cfg_json.get("public_jwk") or cfg_json.get("public_jwk_url")):
            sys.exit("Tool config has neither public_jwk nor public_jwk_url")
        for ext in cfg_json.get("extensions") or []:
            if not ext.get("privacy_level"):
                print(
                    "      WARNING: extensions[].privacy_level is absent. Canvas "
                    "defaults to 'anonymous', which drops the user's name and "
                    "email from the launch and stops $Canvas.user.id expanding."
                )
        print(f"      OK — '{cfg_json.get('title')}'")

        if not args.apply:
            print()
            print("Dry run. Re-run with --apply to make these changes:")
            print(f"  1. create an LTI developer key in account {account} from the config URL")
            print("  2. turn that key ON")
            print(f"  3. deploy it to course {course}")
            print("  4. read back the deployment_id")
            return 0

        # 1. Create the developer key + tool configuration in one call.
        print("[1/4] Creating the LTI developer key...")
        created = _check(
            client.post(
                f"{base}/api/lti/accounts/{account}/developer_keys/tool_configuration",
                # Canvas namespaces this payload: ``settings_url`` lives under
                # ``tool_configuration``, not at the top level. Sending it flat
                # returns 400 "tool_configuration is missing", which reads like
                # a permission problem but isn't.
                json={
                    "tool_configuration": {
                        "settings_url": config_url,
                    },
                    "developer_key": {
                        "name": args.tool_name,
                        # Canvas stores redirect URIs as a newline-delimited
                        # text field. All of them must be listed or the
                        # post-login redirect fails with "Invalid redirect_uri".
                        "redirect_uris": "\n".join(
                            cfg_json.get("redirect_uris") or [cfg_json["target_link_uri"]]
                        ),
                    },
                },
            ),
            "Create developer key",
        )
        dev_key = (created.get("developer_key") or {})
        client_id = str(
            dev_key.get("id")
            or created.get("developer_key_id")
            or (created.get("tool_configuration") or {}).get("developer_key_id")
            or ""
        )
        if not client_id:
            print(json.dumps(created, indent=2)[:1500])
            sys.exit("Could not determine the client_id from the response above.")
        print(f"      client_id = {client_id}")

        # 2. Enable it. A key left off fails launches with no useful message.
        print("[2/4] Enabling the key...")
        _check(
            client.put(
                f"{base}/api/v1/accounts/{account}/developer_keys/{client_id}",
                json={"developer_key": {"workflow_state": "on"}},
            ),
            "Enable developer key",
        )
        print("      on")

        # 3. Deploy into the course. This is what mints the deployment.
        print(f"[3/4] Deploying to course {course}...")
        tool_obj = _check(
            client.post(
                f"{base}/api/v1/courses/{course}/external_tools",
                json={"client_id": client_id},
            ),
            "Install external tool",
        )
        tool_id = tool_obj.get("id")
        print(f"      external tool id = {tool_id}")

        # 4. Read the deployment_id back.
        print("[4/4] Reading deployment_id...")
        deployment_id = str(tool_obj.get("deployment_id") or "")
        if not deployment_id and tool_id:
            detail = _check(
                client.get(f"{base}/api/v1/courses/{course}/external_tools/{tool_id}"),
                "Fetch external tool",
            )
            deployment_id = str(detail.get("deployment_id") or "")
        if not deployment_id:
            print(
                "      Canvas did not return deployment_id on this version.\n"
                f"      Read it manually: {base}/courses/{course}/settings/configurations"
                " -> gear next to the app -> Deployment Id"
            )

    print()
    print("=" * 68)
    print("Add these to /opt/reflow/reflow-canvas-lti/.env")
    print("=" * 68)
    print(f"LTI_CLIENT_ID={client_id}")
    if deployment_id:
        print(f"LTI_DEPLOYMENT_ID={deployment_id}")
    else:
        print("LTI_DEPLOYMENT_ID=<read from the Canvas UI, see above>")
    print("MULTI_TENANT_WATCHER=true")
    print("CANVAS_WATCHED_COURSES=")
    print()
    print("Still manual — Canvas has no API to mint an API Key, and its secret")
    print("is displayed only once:")
    print(f"  {base}/accounts/{account}/developer_keys")
    print("  + Developer Key -> + API Key")
    print(f"  Redirect URI: {tool}/canvas/oauth/callback")
    print("  Then set CANVAS_OAUTH_CLIENT_ID and CANVAS_OAUTH_CLIENT_SECRET.")
    print()
    print("Then: sudo docker compose restart connector, and launch the tool")
    print(f"from course {course}. Look for 'platform upsert' in the logs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
