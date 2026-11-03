"""Persist monitor state on a separate Git branch without changing the checkout."""

import argparse
import subprocess
from pathlib import Path

BRANCH = "refs/heads/gacha-monitor-state"
LOCAL_REF = "refs/remotes/origin/gacha-monitor-state"
STATE = Path(".local/state.json")
BASE = Path(".local/state-base")


def git(*args, input=None, allowed=(0,)):
    result = subprocess.run(["git", *args], input=input, capture_output=True)
    if result.returncode not in allowed:
        raise RuntimeError(f"Git {args[0]} failed (exit {result.returncode}). State was not reset; check repository permissions and branch protection.")
    return result


def load():
    STATE.parent.mkdir(parents=True, exist_ok=True)
    # Exit 2 means a successful remote query found no matching branch.
    found = git("ls-remote", "--exit-code", "--heads", "origin", BRANCH, allowed=(0, 2))
    if found.returncode == 2:
        if STATE.exists():
            raise RuntimeError("Remote state branch is missing but local state exists; refusing to discard it.")
        BASE.write_text("", encoding="ascii")
        return
    git("fetch", "--no-tags", "origin", f"{BRANCH}:{LOCAL_REF}")
    commit = git("rev-parse", LOCAL_REF).stdout.decode().strip()
    contents = git("show", f"{commit}:state.json").stdout
    STATE.write_bytes(contents)
    BASE.write_text(commit, encoding="ascii")


def save():
    if not STATE.exists() or not BASE.exists():
        raise RuntimeError("Load state and successfully run the monitor before saving.")
    parent = BASE.read_text(encoding="ascii").strip()
    blob = git("hash-object", "-w", str(STATE)).stdout.decode().strip()
    tree = git("mktree", input=f"100644 blob {blob}\tstate.json\n".encode()).stdout.decode().strip()
    if parent and git("rev-parse", f"{parent}^{{tree}}").stdout.decode().strip() == tree:
        print("State unchanged; no commit needed.")
        return
    args = ["-c", "user.name=github-actions[bot]", "-c",
            "user.email=41898282+github-actions[bot]@users.noreply.github.com", "commit-tree", tree]
    if parent:
        args.extend(["-p", parent])
    commit = git(*args, input=b"Update gashapon monitor state\n").stdout.decode().strip()
    # Never force: concurrent changes must fail rather than lose notification history.
    git("push", "origin", f"{commit}:{BRANCH}")
    print("Monitor state saved on gacha-monitor-state.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("load", "save"))
    args = parser.parse_args()
    try:
        {"load": load, "save": save}[args.operation]()
    except (RuntimeError, OSError) as error:
        print(f"State storage error: {error}")
        raise SystemExit(1)
