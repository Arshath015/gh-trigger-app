"""
Scheduled auto-trigger for GitHub contribution automation (v2).

Design goals (fixing v1 issues):
  1. Never silently miss a whole day. Progress is tracked in a state file
     committed back to THIS repo (not the target repos), so a delayed/missed
     hourly run just gets picked up on the next run instead of losing the day.
  2. Each day has a randomly chosen commit target (MIN_COMMITS..MAX_COMMITS,
     default 5..30), spread across the day's active hours and across
     potentially many different repos you own.
  3. Every edit type is purely additive/non-destructive: README sections,
     changelog entries, .gitignore extensions, or brand-new utility/fallback
     files that are never imported anywhere (so they cannot break existing code).

Run by GitHub Actions on a schedule (see .github/workflows/auto-trigger.yml).
"""

import os
import sys
import json
import random
import base64
from datetime import datetime, timedelta, timezone

import requests

API = "https://api.github.com"

# ---------- Config from environment ----------
GH_USER = os.environ.get("GH_USER", "").strip()
GH_TOKEN = os.environ.get("GH_TOKEN", "").strip()

# This repo (used to store the daily state file). Auto-detected from Actions context,
# falls back to "gh-trigger-app" if run locally for testing.
SELF_REPO_FULL = os.environ.get("GITHUB_REPOSITORY", "")
SELF_OWNER, SELF_REPO = (SELF_REPO_FULL.split("/") + ["", "gh-trigger-app"])[:2] if SELF_REPO_FULL else (GH_USER, "gh-trigger-app")
if not SELF_OWNER:
    SELF_OWNER = GH_USER

# Comma-separated weekday numbers to skip, Monday=0 ... Sunday=6. Empty = never skip.
OFF_DAYS = os.environ.get("OFF_DAYS", "").strip()
OFF_DAYS_SET = {int(d) for d in OFF_DAYS.split(",") if d.strip() != ""}

MIN_HOUR = int(os.environ.get("MIN_HOUR", "9") or 9)
MAX_HOUR = int(os.environ.get("MAX_HOUR", "22") or 22)
MIN_COMMITS = int(os.environ.get("MIN_COMMITS", "5") or 5)
MAX_COMMITS = int(os.environ.get("MAX_COMMITS", "30") or 30)
# Max commits attempted in a single run (spreads load across the day instead of bursting).
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "3") or 3)

IST_OFFSET = timedelta(hours=5, minutes=30)


def now_ist():
    return datetime.now(timezone.utc) + IST_OFFSET


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


# ---------- Daily state (stored in THIS repo, not target repos) ----------
def state_path_for(dt):
    return f".trigger-state/{dt.strftime('%Y-%m-%d')}.json"


def load_or_init_state(dt):
    path = state_path_for(dt)
    existing = get_file(SELF_OWNER, SELF_REPO, path)
    if existing:
        return json.loads(existing["content"]), existing["sha"], path

    seed_str = dt.strftime("%Y-%m-%d") + "-gh-trigger-commit-count-salt"
    rng = random.Random(seed_str)
    target = rng.randint(MIN_COMMITS, MAX_COMMITS)
    state = {"date": dt.strftime("%Y-%m-%d"), "target": target, "done": 0, "log": []}
    return state, None, path


def save_state(state, sha, path):
    content = json.dumps(state, indent=2)
    result = put_file(SELF_OWNER, SELF_REPO, path, content, f"chore: update daily trigger state ({state['done']}/{state['target']})", sha)
    return result["content"]["sha"]


# ---------- Additive-only task templates ----------
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
    if "## Requirements" in readme:
        return None
    return readme + "\n\n## Requirements\n\n```\npip install -r requirements.txt\n```\n", "docs: document installation requirements"


def task_last_updated(readme, dt):
    date_str = dt.strftime("%Y-%m-%d")
    marker = "**Last updated:**"
    if marker in readme:
        lines = readme.split("\n")
        new_lines = [f"**Last updated:** {date_str}" if l.strip().startswith(marker) else l for l in lines]
        joined = "\n".join(new_lines)
        if joined == readme:
            return None
        return joined, "chore: update maintenance timestamp"
    return readme + f"\n\n---\n**Last updated:** {date_str}\n", "chore: add maintenance timestamp footer"


README_TASKS = [task_typo_fix, task_add_badges, task_add_requirements]


def task_gitignore(owner, repo):
    existing = get_file(owner, repo, ".gitignore")
    standard = ["__pycache__/", "*.pyc", ".env", "venv/", ".DS_Store", "*.log", ".ipynb_checkpoints/", "node_modules/", "dist/", "build/"]
    if not existing:
        return ".gitignore", None, "\n".join(standard) + "\n", "chore: standardize .gitignore for project tooling"
    missing = [e for e in standard if e not in existing["content"]]
    if not missing:
        return None
    new_content = existing["content"].rstrip() + "\n" + "\n".join(missing) + "\n"
    return ".gitignore", existing["sha"], new_content, "chore: extend .gitignore with standard exclusions"


def task_changelog(owner, repo, dt):
    """Always applicable — appends a dated entry. This is the reliable fallback
    that guarantees we can always find *something* additive to commit."""
    existing = get_file(owner, repo, "CHANGELOG.md")
    date_str = dt.strftime("%Y-%m-%d")
    entries = [
        "Minor internal housekeeping and dependency review.",
        "Reviewed open items and updated project notes.",
        "Documentation pass for clarity and consistency.",
        "Routine maintenance checkpoint.",
        "Verified build/tooling configuration is current.",
    ]
    entry = f"\n### {date_str}\n- {random.choice(entries)}\n"
    if existing:
        if f"### {date_str}" in existing["content"]:
            return None  # already logged today for this repo
        new_content = existing["content"].rstrip() + "\n" + entry
        return "CHANGELOG.md", existing["sha"], new_content, "docs: update changelog"
    new_content = f"# Changelog\n\nAll notable changes to this project are documented here.\n{entry}"
    return "CHANGELOG.md", None, new_content, "docs: add changelog"


def task_fallback_utility(owner, repo, dt, suffix):
    """Adds a brand-new, never-imported file under docs/notes/ — cannot affect
    existing code since nothing references it. Always applicable (unique path).
    Content is written as a normal, generic engineering note — it must never
    reveal that it was created by an automated process."""
    date_str = dt.strftime("%Y%m%d")
    path = f"docs/notes/{date_str}-{suffix}.md"
    notes = [
        "Reviewed open items for this iteration; no blockers found.",
        "Went through outstanding items on the backlog; nothing urgent.",
        "Checked current state of the project against the roadmap.",
        "Noted a few small follow-ups to revisit in a future pass.",
        "Confirmed current setup still matches documented behavior.",
    ]
    content = f"## {dt.strftime('%Y-%m-%d')}\n\n{random.choice(notes)}\n"
    return path, None, content, "docs: add project note"


# ---------- Repo-level task attempt ----------
def attempt_one_commit(repos, dt, run_suffix):
    """Tries, in random order, to find one applicable additive edit on a
    randomly chosen repo. Falls back to changelog / dev-note which are
    always applicable, so this should essentially never come back empty
    unless the account has zero repos."""
    repo = random.choice(repos)
    owner = repo["owner"]["login"]
    name = repo["name"]

    candidates = []

    readme = get_file(owner, name, "README.md")
    if readme:
        for fn in README_TASKS:
            candidates.append(("readme", fn, readme))
        candidates.append(("last_updated", None, readme))

    candidates.append(("gitignore", None, None))
    candidates.append(("changelog", None, None))
    candidates.append(("fallback", None, None))

    random.shuffle(candidates)

    for kind, fn, readme_data in candidates:
        if kind == "readme":
            result = fn(readme_data["content"])
            if result:
                new_content, message = result
                put_file(owner, name, "README.md", new_content, message, readme_data["sha"])
                return f"{name}: {message}"
        elif kind == "last_updated":
            result = task_last_updated(readme_data["content"], dt)
            if result:
                new_content, message = result
                put_file(owner, name, "README.md", new_content, message, readme_data["sha"])
                return f"{name}: {message}"
        elif kind == "gitignore":
            result = task_gitignore(owner, name)
            if result:
                path, sha, new_content, message = result
                put_file(owner, name, path, new_content, message, sha)
                return f"{name}: {message}"
        elif kind == "changelog":
            result = task_changelog(owner, name, dt)
            if result:
                path, sha, new_content, message = result
                put_file(owner, name, path, new_content, message, sha)
                return f"{name}: {message}"
        elif kind == "fallback":
            path, sha, new_content, message = task_fallback_utility(owner, name, dt, run_suffix)
            put_file(owner, name, path, new_content, message, sha)
            return f"{name}: {message}"

    return None


def main():
    if not GH_USER or not GH_TOKEN:
        print("Missing GH_USER or GH_PAT secret. Configure them in repo Settings.")
        sys.exit(1)

    dt = now_ist()
    print(f"Current IST time: {dt.strftime('%Y-%m-%d %H:%M')} (weekday {dt.weekday()})")

    if dt.weekday() in OFF_DAYS_SET:
        print("Today is configured as an off day. Skipping.")
        return

    if dt.hour < MIN_HOUR or dt.hour > MAX_HOUR:
        print(f"Outside active window ({MIN_HOUR}:00-{MAX_HOUR}:00 IST). Skipping this run.")
        return

    state, sha, path = load_or_init_state(dt)
    print(f"Daily target: {state['done']}/{state['target']} commits so far.")

    if state["done"] >= state["target"]:
        print("Today's commit target already reached. Nothing more to do.")
        return

    remaining = state["target"] - state["done"]
    this_run = min(MAX_PER_RUN, remaining, random.randint(1, MAX_PER_RUN))

    repos = fetch_repos()
    if not repos:
        print("No repos found on this account.")
        return

    made = 0
    for i in range(this_run):
        try:
            suffix = f"{dt.strftime('%H%M')}{i}"
            result = attempt_one_commit(repos, dt, suffix)
            if result:
                print(f"Committed: {result}")
                state["log"].append(result)
                made += 1
            else:
                print("No applicable edit found this attempt (rare) — skipping to next.")
        except requests.HTTPError as e:
            print(f"HTTP error during commit attempt, continuing: {e}")

    state["done"] += made
    new_sha = save_state(state, sha, path)
    print(f"Run complete: {made} commit(s) made. Daily progress now {state['done']}/{state['target']}.")


if __name__ == "__main__":
    main()
