# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""Smoke test for the API endpoints the n8n workflows on vault depend on.

Run against a throwaway instance, never the live one:

    uv run smoke/n8n_contract.py --base-url http://localhost:17442 --setup

--setup configures a fresh instance (multi-user mode, API key, admin user)
through the built-in admin token. The values below are test-only defaults.
"""

import argparse
import sys
import time

import httpx

# Built-in admin token from backend/app.js; accepted as an apiKey on any instance.
ADMIN_TOKEN = "4241b401-7236-493e-92b5-b72696b9d853"
TEST_API_KEY = "00000000-0000-4000-8000-000000000001"
TEST_ADMIN_PASSWORD = "smoke-test-only"

VIDEO_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # "Me at the zoo", 19 s

failures = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(name)
    return ok


def setup(client):
    q = {"apiKey": ADMIN_TOKEN}
    config = client.get("/api/config", params=q).json()["config_file"]
    config["YoutubeDLMaterial"]["API"]["use_API_key"] = True
    config["YoutubeDLMaterial"]["API"]["API_key"] = TEST_API_KEY
    config["YoutubeDLMaterial"]["Advanced"]["multi_user_mode"] = True
    r = client.post("/api/setConfig", params=q, json={"new_config_file": config})
    check("setup: setConfig", r.status_code == 200 and r.json().get("success"))
    r = client.post(
        "/api/auth/register",
        params=q,
        json={"userid": "admin", "username": "admin", "password": TEST_ADMIN_PASSWORD},
    )
    # 409 means the admin already exists from an earlier run
    check("setup: register admin", r.status_code in (200, 409), str(r.status_code))


def wait_for_download(client, auth, download_uid, timeout=300):
    """Poll /api/download the way the ytdlm-getID node does."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.post("/api/download", params=auth, json={"download_uid": download_uid})
        download = r.json().get("download")
        if download and (download.get("finished") or download.get("error")):
            return download
        time.sleep(3)
    return None


def start_download(client, auth, name, body):
    r = client.post("/api/downloadFile", params=auth, json=body)
    if not check(f"{name}: /api/downloadFile", r.status_code == 200, str(r.status_code)):
        return None
    uid = r.json()["download"]["uid"]
    download = wait_for_download(client, auth, uid)
    ok = bool(download) and download.get("finished") and not download.get("error")
    check(f"{name}: /api/download finished", ok, str(download and download.get("error")))
    return download if ok else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:17442")
    parser.add_argument("--api-key", default=TEST_API_KEY)
    parser.add_argument("--password", default=TEST_ADMIN_PASSWORD)
    parser.add_argument("--setup", action="store_true")
    args = parser.parse_args()

    client = httpx.Client(base_url=args.base_url, timeout=60)
    if args.setup:
        setup(client)

    # Get YTDLM Token node: form body, apiKey in query
    r = client.post(
        "/api/auth/login",
        params={"apiKey": args.api_key},
        data={"username": "admin", "password": args.password},
    )
    token = r.status_code == 200 and r.json().get("token")
    if not check("/api/auth/login returns token", bool(token), str(r.status_code)):
        return 1
    auth = {"apiKey": args.api_key, "jwt": token}

    # default output template is truncated to 150 bytes
    r = client.post("/api/generateArgs", params=auth, json={"url": VIDEO_URL, "type": "video"})
    out = r.json()["args"][r.json()["args"].index("-o") + 1]
    check("default output uses %(title).150B", "%(title).150B" in out, out)

    # ytdlm-normal node
    start_download(
        client, auth, "video",
        {"url": VIDEO_URL, "youtubeAuthConfig": {"extraArguments": ["--restrict-filenames"]}},
    )

    # Retry with ignoreArchive / ytdlm-fallback-rename nodes
    start_download(
        client, auth, "audio+customOutput",
        {
            "url": VIDEO_URL,
            "type": "audio",
            "ignoreArchive": True,
            "customOutput": "fb_vid_%(epoch)s.%(ext)s",
            "youtubeAuthConfig": {"extraArguments": ["--restrict-filenames", "--no-write-thumbnail"]},
        },
    )

    # get recent vid UID node
    r = client.post(
        "/api/getAllFiles", params=auth,
        json={"sort": {"by": "registered", "order": -1}, "range": [0, 1]},
    )
    files = r.status_code == 200 and r.json().get("files")
    if check("/api/getAllFiles returns newest file", bool(files), str(r.status_code)):
        # DL file from server node: form body
        r = client.post("/api/downloadFileFromServer", params=auth, data={"uid": files[0]["uid"]})
        check(
            "/api/downloadFileFromServer returns bytes",
            r.status_code == 200 and len(r.content) > 10_000,
            f"{r.status_code}, {len(r.content)} bytes, {files[0].get('title')}",
        )

    print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
