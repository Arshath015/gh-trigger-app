"""
Scheduled auto-trigger for GitHub contribution automation.

This script is run hourly by GitHub Actions (see .github/workflows/auto-trigger.yml).
Each run, it decides two things purely from the date (so it's consistent across the
hour it's checked, but different every day):

  1. Is today an "off day"? (configurable via the OFF_DAYS repo variable)
  2. What single hour today is the "chosen" hour to actually commit?
     (deterministically pseudo-random, seeded by the date)

If the current hour matches the chosen hour and today isn't an off day, it performs
one real, small, genuine edit on a randomly selected repo you own, and commits it.
Otherwise it exits without doing anything (this is expected and normal for 23 out
of 24 runs per day).
"""

import os
import random
import base64
import sys
from datetime import datetime, timedelta, timezone

import requests

API = "https://api.github.com"

# ---------- Config from environment ----------
GH_USER = os.environ.get("GH_USER", "").strip()
GH_TOKEN = os.environ.get("GH_TOKEN", "").strip()

# Comma-separated weekday numbers to skip, Monday=0 ... Sunday=6.
# Default: Sunday off. Change anytime in GitHub:
# Settings -> Secrets and variables -> Actions -> Variables -> OFF_DAYS
# e.g. "6" (Sunday only), "2,6" (Wednesday + Sunday), "" (never take a day off)
OFF_DAYS = os.environ.get("OFF_DAYS", "6").strip()
OFF_DAYS_SET = {int(d) for d in OFF_DAYS.split(",") if d.strip() != ""}

# The random "active hour" for a trigger day is chosen within this window,
# in IST (India Standard Time), so you don't get 3am commits.
MIN_HOUR = int(os.environ.get("MIN_HOUR", "9") or 9)
MAX_HOUR = int(os.environ.get("MAX_HOUR", "22") or 22)

IST_OFFSET = timedelta(hours=5, minutes=30)


def now_ist():
    return datetime.now(timezone.utc) + IST_OFFSET


def is_off_day(dt):
    return dt.weekday() in OFF_DAYS_SET


def chosen_hour_for_date(dt):
    """Deterministic pseudo-random hour for this specific date, so repeated
    checks within the same day always agree, but each day gets a different hour."""
    seed_str = dt.strftime("%Y-%m-%d") + "-gh-trigger-salt"
    rng = random.Random(seed_str)
    return rng.randint(MIN_HOUR, MAX_HOUR)


def auth_headers():
    return {"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github+json"}


def fetch_repos():
    res = requests.get(
        f"{API}/user/repos", headers=auth_headers(), params={"per_page": 100, "affiliation": "owner"}
    )
    res.raise_for_status()
    return [r for r in res.json() if not r.get("archived")]


def get_file(owner, repo, path):
    res = requests.get(f"{API}/repos/{owner}/{repo}/contents/{path}", headers=auth_headers())
    if res.status_code == 404:
        return None
    res.raise_for_status()
    data = res.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return {"sha": data["sha"], "content": content}


def put_file(owner, repo, path, content, message, sha=None):
    body = {"message": message, "content": base64.b64encode(content.encode("utf-8")).decode("utf-8")}
    if sha:
        body["sha"] = sha
    res = requests.put(f"{API}/repos/{owner}/{repo}/contents/{path}", headers=auth_headers(), json=body)
    res.raise_for_status()
    return res.json()


# ---------- Micro-task templates (mirrors the mobile app's README tasks) ----------
TYPOS = {
    "teh ": "the ", "wich ": "which ", "occured": "occurred", "seperate": "separate",
    "recieve": "receive", "adress": "address", "untill": "until", "alot ": "a lot ", "thier ": "their ",
}


def task_typo_fix(readme):
    for wrong, right in TYPOS.items():
        if wrong in readme:
            return readme.replace(wrong, right), "docs: correct terminology in documentation"
    return None


def task_add_badges(readme):
    if "shields.io" in readme:
        return None
    badge = "![Python](https://img.shields.io/badge/python-3.x-blue.svg)"
    lines = readme.split("\n")
    insert_at = 1 if lines and lines[0].startswith("#") else 0
    lines[insert_at:insert_at] = ["", badge]
    return "\n".join(lines), "docs: add technology badges to project header"


def task_add_requirements(readme):
    if "## Requirements" in readme or "##Requirements" in readme:
        return None
    return readme + "\n\n## Requirements\n\n```\npip install -r requirements.txt\n```\n", "docs: document installation requirements"


def task_last_updated(readme):
    date_str = now_ist().strftime("%Y-%m-%d")
    marker = "**Last updated:**"
    if marker in readme:
        lines = readme.split("\n")
        new_lines = [f"**Last updated:** {date_str}" if l.strip().startswith(marker) else l for l in lines]
        return "\n".join(new_lines), "chore: update maintenance timestamp"
    return readme + f"\n\n---\n**Last updated:** {date_str}\n", "chore: add maintenance timestamp footer"


README_TASKS = [task_typo_fix, task_add_badges, task_add_requirements, task_last_updated]


def task_gitignore(owner, repo):
    existing = get_file(owner, repo, ".gitignore")
    standard = ["__pycache__/", "*.pyc", ".env", "venv/", ".DS_Store", "*.log", ".ipynb_checkpoints/"]
    if not existing:
        return ".gitignore", None, "\n".join(standard) + "\n", "chore: standardize .gitignore for Python tooling"
    missing = [e for e in standard if e not in existing["content"]]
    if not missing:
        return None
    new_content = existing["content"].rstrip() + "\n" + "\n".join(missing) + "\n"
    return ".gitignore", existing["sha"], new_content, "chore: extend .gitignore with standard exclusions"


def run_trigger():
    repos = fetch_repos()
    if not repos:
        print("No repos found.")
        return

    repo = random.choice(repos)
    owner = repo["owner"]["login"]
    name = repo["name"]

    if random.random() < 0.3:
        result = task_gitignore(owner, name)
        if result:
            path, sha, new_content, message = result
            put_file(owner, name, path, new_content, message, sha)
            print(f"Committed to {name}: {message}")
            return

    readme = get_file(owner, name, "README.md")
    if not readme:
        print(f"{name} has no README.md, skipping this run.")
        return

    tasks = README_TASKS[:]
    random.shuffle(tasks)
    for task_fn in tasks:
        result = task_fn(readme["content"])
        if result:
            new_content, message = result
            put_file(owner, name, "README.md", new_content, message, readme["sha"])
            print(f"Committed to {name}: {message}")
            return

    print(f"No applicable edits found for {name} this run.")


def main():
    if not GH_USER or not GH_TOKEN:
        print("Missing GH_USER or GH_PAT secret. Configure them in repo Settings.")
        sys.exit(1)

    dt = now_ist()
    print(f"Current IST time: {dt.strftime('%Y-%m-%d %H:%M')} (weekday {dt.weekday()})")

    if is_off_day(dt):
        print("Today is configured as an off day. Skipping.")
        return

    target_hour = chosen_hour_for_date(dt)
    print(f"Chosen active hour for today: {target_hour}:00 IST")

    if dt.hour != target_hour:
        print("Not the chosen hour yet. Skipping this run.")
        return

    print("This is the chosen hour — running trigger now.")
    run_trigger()


if __name__ == "__main__":
    main()
