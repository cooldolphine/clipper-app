#!/usr/bin/env python3
"""Upload queued clips to YouTube. Runs every 15 minutes on GitHub Actions.

queue.json   - written by the phone app (what to post, with title/description/tags/time)
status.json  - written by this script (what happened)

Scheduled clips are uploaded right away as PRIVATE with a publish time, and YouTube
makes them public at exactly that time. Clips with no time are uploaded as public.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

QUEUE, STATUS = Path("queue.json"), Path("status.json")
MAX_ATTEMPTS = 3


def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def utcnow():
    return datetime.now(timezone.utc)


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True)


def save_status(status):
    """Write status.json and push it, so a clip is never uploaded twice."""
    STATUS.write_text(json.dumps(status, indent=1, ensure_ascii=False), encoding="utf-8")
    git("config", "user.name", "clipper-bot")
    git("config", "user.email", "bot@users.noreply.github.com")
    git("add", "status.json")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    git("commit", "-m", "update post status")
    for _ in range(4):
        git("pull", "--rebase", "--autostash")
        if git("push").returncode == 0:
            return
    print("  warning: could not push status.json")


def err_text(r):
    try:
        e = r.json()["error"]
        reason = (e.get("errors") or [{}])[0].get("reason", "")
        return f"YouTube error {r.status_code}: {e.get('message', '')} ({reason})"
    except Exception:
        return f"YouTube error {r.status_code}: {r.text[:200]}"


def access_token():
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": os.environ["YT_CLIENT_ID"],
        "client_secret": os.environ["YT_CLIENT_SECRET"],
        "refresh_token": os.environ["YT_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }, timeout=30)
    j = r.json()
    if "access_token" not in j:
        raise RuntimeError("YouTube sign-in failed: " + j.get("error_description", j.get("error", "unknown")))
    return j["access_token"]


def fetch_clip(item, dest):
    """Download the clip from the (possibly private) clipper repo's release."""
    repo = item["repo"]
    h = {"Authorization": f"Bearer {os.environ['CLIPPER_READ_TOKEN']}", "Accept": "application/vnd.github+json"}
    rel = requests.get(f"https://api.github.com/repos/{repo}/releases/tags/run-{item['run']}", headers=h, timeout=30)
    if rel.status_code == 404:
        raise RuntimeError(f"Release run-{item['run']} was not found (old releases are deleted automatically).")
    rel.raise_for_status()
    asset = next((a for a in rel.json()["assets"] if a["name"] == item["file"]), None)
    if not asset:
        raise RuntimeError(f"Clip {item['file']} is not in release run-{item['run']}.")
    with requests.get(asset["url"], headers={**h, "Accept": "application/octet-stream"}, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)


def clean_tags(tags):
    out, total, seen = [], 0, set()
    for t in tags:
        t = re.sub(r"[<>]", "", str(t)).strip()
        if not t or t.lower() in seen:
            continue
        cost = len(t) + (2 if " " in t else 0) + 1
        if total + cost > 480:
            break
        out.append(t)
        seen.add(t.lower())
        total += cost
    return out


def upload(item, path, token):
    when = parse_time(item["when"]) if item.get("when") else None
    scheduled = bool(when and when > utcnow() + timedelta(minutes=2))
    state = {"privacyStatus": "private" if scheduled else "public", "selfDeclaredMadeForKids": False}
    if scheduled:
        state["publishAt"] = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    title = re.sub(r"[<>]", "", item["title"]).strip()[:100]
    if not title:
        raise RuntimeError("The title is empty.")
    body = {
        "snippet": {
            "title": title,
            "description": re.sub(r"[<>]", "", item.get("description", ""))[:4800],
            "tags": clean_tags(item.get("tags", [])),
            "categoryId": "20",  # Gaming
        },
        "status": state,
    }
    size = path.stat().st_size
    init = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos",
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"},
        data=json.dumps(body), timeout=60)
    if init.status_code != 200:
        raise RuntimeError(err_text(init))
    with open(path, "rb") as f:
        up = requests.put(init.headers["Location"], data=f,
                          headers={"Content-Length": str(size), "Content-Type": "video/mp4"}, timeout=900)
    if up.status_code not in (200, 201):
        raise RuntimeError(err_text(up))
    return up.json()["id"], scheduled, state.get("publishAt")


def attempts_so_far(item, status):
    st = status.get(item["id"], {})
    return st.get("attempts", 0) if st.get("rev") == item.get("rev", 1) else 0


def todo(item, status):
    if item.get("platform") != "youtube":
        return False
    if status.get(item["id"], {}).get("state") == "uploaded":
        return False
    return attempts_so_far(item, status) < MAX_ATTEMPTS


def main():
    queue, status = load(QUEUE, []), load(STATUS, {})
    live = {q["id"] for q in queue}
    status = {k: v for k, v in status.items() if k in live}  # forget removed items
    work = [q for q in queue if todo(q, status)]
    print(f"{len(queue)} in the queue, {len(work)} to upload")
    if not work:
        if status != load(STATUS, {}):
            save_status(status)
        return 0

    failed = 0
    try:
        token = access_token()
    except Exception as e:
        for it in work:
            status[it["id"]] = {"state": "failed", "error": str(e)[:300], "rev": it.get("rev", 1),
                                "attempts": attempts_so_far(it, status) + 1}
        save_status(status)
        print(e)
        return 1

    for it in work:
        print(f"\nUploading: {it['title']}")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                clip = Path(tmp) / "clip.mp4"
                fetch_clip(it, clip)
                vid, scheduled, publish_at = upload(it, clip, token)
            status[it["id"]] = {"state": "uploaded", "video_id": vid, "url": f"https://youtu.be/{vid}",
                                "scheduled": scheduled, "publish_at": publish_at, "rev": it.get("rev", 1),
                                "uploaded_at": utcnow().isoformat(), "title": it.get("title", ""),
                                "meta": it.get("meta") or {}}
            print(f"  done: https://youtu.be/{vid}" + (f" (goes public {publish_at})" if scheduled else ""))
        except Exception as e:
            failed += 1
            status[it["id"]] = {"state": "failed", "error": str(e)[:300], "rev": it.get("rev", 1),
                                "attempts": attempts_so_far(it, status) + 1}
            print(f"  failed: {e}")
        save_status(status)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
