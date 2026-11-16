"""Verify private release assets and publish each due project commit."""
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
from subprocess import check_output, run
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / ".github" / "publication.json"
TAG = "scheduled-publication"


def git(*args):
    result = run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True)
    return result.stdout


def api(endpoint, *args):
    return check_output(["gh", "api", endpoint, *args], cwd=ROOT)


def release_assets(repo):
    releases = json.loads(api(f"repos/{repo}/releases?per_page=100"))
    release = next(release for release in releases if release["tag_name"] == TAG and release["draft"])
    return {asset["name"]: asset["id"] for asset in release["assets"]}


def read_batch(contents, phase):
    if sha256(contents).hexdigest() != phase["sha256"]:
        raise RuntimeError(f"Asset checksum mismatch for {phase['asset']}.")
    with ZipFile(BytesIO(contents)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or sorted(names) != sorted(phase["files"]):
            raise RuntimeError(f"Unexpected file paths in {phase['asset']}.")
        batch = {}
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or set(path.parts) & {"..", ".git", ".github"}:
                raise RuntimeError(f"Disallowed staged path {name}.")
            batch[name] = archive.read(name)
        return batch


def publish_batch(phase, batch):
    # Check the full batch before writing. An intervening edit must stay intact.
    pending = {}
    base = phase.get("base_sha256", {})
    for name, contents in batch.items():
        target = ROOT / name
        if not target.resolve().is_relative_to(ROOT):
            raise RuntimeError(f"Disallowed destination {name}.")
        if target.exists():
            current = target.read_bytes()
            if current == contents:
                continue
            if sha256(current).hexdigest() != base.get(name):
                raise RuntimeError(f"{name} already exists with different content.")
        elif name in base:
            raise RuntimeError(f"The expected original {name} is missing.")
        pending[name] = contents
    for name, contents in pending.items():
        target = ROOT / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
    if pending:
        git("add", "--", *pending)
        git("commit", "-m", phase["message"])
        print(f"Committed {phase['asset']}.")
    else:
        print(f"Already published {phase['asset']}.")


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    verify = os.environ["PUBLICATION_EVENT"] == "workflow_dispatch"
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    assets = release_assets(repo)
    now = datetime.now(timezone.utc)
    due = [phase for phase in plan if now >= datetime.fromisoformat(phase["due"])]
    if not verify and git("status", "--porcelain").strip():
        raise RuntimeError("The publication checkout has unexpected changes.")
    if not verify:
        git("config", "user.name", "github-actions[bot]")
        git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    for phase in plan if verify else due:
        asset_id = assets[phase["asset"]]
        contents = api(f"repos/{repo}/releases/assets/{asset_id}",
                       "-H", "Accept: application/octet-stream")
        batch = read_batch(contents, phase)
        print(f"Verified {phase['asset']} ({len(batch)} files).")
        if not verify:
            publish_batch(phase, batch)
    if not verify:
        git("push", "origin", "HEAD:main")
        if len(due) == len(plan):
            api(f"repos/{repo}/actions/workflows/scheduled-commits.yml/disable", "-X", "PUT")
            print("Publication complete. Schedule disabled.")


if __name__ == "__main__":
    main()
