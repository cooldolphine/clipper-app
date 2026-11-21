#!/usr/bin/env python3
"""Collect views / likes / comments of the clips you uploaded -> performance.json.

The clipper reads performance.json before picking moments, so it learns what works on
your channel. Uses only public numbers, with a YouTube API key (YT_API_KEY secret).
Checks at most every 3 hours.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

STATUS, QUEUE, PERF = Path("status.json"), Path("queue.json"), Path("performance.json")
EVERY = timedelta(hours=3)


def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def utcnow():
    return datetime.now(timezone.utc)


def parse(s):
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True)


def save(perf):
    PERF.write_text(json.dumps(perf, indent=1, ensure_ascii=False), encoding="utf-8")
    git("config", "user.name", "clipper-bot")
    git("config", "user.email", "bot@users.noreply.github.com")
    git("add", "performance.json")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    git("commit", "-m", "update performance numbers")
    for _ in range(4):
        git("pull", "--rebase", "--autostash")
        if git("push").returncode == 0:
            return
    print("warning: could not push performance.json")


def main():
    key = os.environ.get("YT_API_KEY")
    if not key:
        print("No YT_API_KEY secret, skipping the performance check.")
        return 0
    perf = load(PERF, {"updated_at": None, "videos": []})
    if perf.get("updated_at") and utcnow() - parse(perf["updated_at"]) < EVERY:
        print("Checked recently, skipping.")
        return 0

    status, queue = load(STATUS, {}), {q["id"]: q for q in load(QUEUE, [])}
    known = {v["video_id"]: v for v in perf.get("videos", [])}
    for qid, st in status.items():
        if st.get("state") != "uploaded" or not st.get("video_id"):
            continue
        item = queue.get(qid, {})
        published = st.get("publish_at") if st.get("scheduled") and st.get("publish_at") else st.get("uploaded_at")
        entry = known.setdefault(st["video_id"], {"video_id": st["video_id"]})
        entry.update(title=st.get("title") or item.get("title", ""), meta=st.get("meta") or item.get("meta") or {},
                     published_at=published)

    live = [v for v in known.values() if v.get("published_at") and parse(v["published_at"]) <= utcnow()]
    ids = [v["video_id"] for v in live]
    for i in range(0, len(ids), 50):
        r = requests.get("https://www.googleapis.com/youtube/v3/videos",
                         params={"part": "statistics", "id": ",".join(ids[i:i + 50]), "key": key}, timeout=30)
        if r.status_code != 200:
            print(f"YouTube answered {r.status_code}: {r.text[:200]}")
            return 1
        for item in r.json().get("items", []):
            s = item.get("statistics", {})
            e = known[item["id"]]
            e.update(views=int(s.get("viewCount", 0)), likes=int(s.get("likeCount", 0)),
                     comments=int(s.get("commentCount", 0)), checked_at=utcnow().isoformat())
    perf["videos"] = sorted(known.values(), key=lambda v: v.get("published_at") or "", reverse=True)
    perf["updated_at"] = utcnow().isoformat()
    print(f"Updated numbers for {len(ids)} videos.")
    save(perf)
    return 0


if __name__ == "__main__":
    sys.exit(main())
