#!/usr/bin/env python3
"""Download an Outpost task as an eval fixture and unpack it into eval/fixtures/<name>/.

  python eval/export_task.py --url https://outpost.internal --token "$ADMIN_TOKEN" --task <task id> [--name my-case]

Find task ids on the dashboard (/admin: "Export fixture" on failed or 👎 tasks) or with GET /api/tasks.
The case is a draft: complete `expect` and delete `draft: true`, then run eval/run.py --case <name>.
Standard library only.
"""
from __future__ import annotations

import argparse
import io
import ssl
import sys
import tarfile
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]


def safe_extract(data: bytes, dest: Path) -> str:
    """Unpack regular files only, refusing absolute paths and `..`. Returns the fixture directory name."""
    names = set()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            path = PurePosixPath(member.name)
            if not member.isfile() or path.is_absolute() or ".." in path.parts or len(path.parts) < 2:
                continue
            names.add(path.parts[0])
            target = dest.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            fh = tar.extractfile(member)
            if fh is not None:
                target.write_bytes(fh.read())
    if len(names) != 1:
        raise SystemExit(f"unexpected archive layout: {sorted(names)}")
    return names.pop()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="orchestrator base URL")
    ap.add_argument("--token", required=True, help="the orchestrator's ADMIN_TOKEN")
    ap.add_argument("--task", required=True, help="task id")
    ap.add_argument("--name", default="", help="fixture name (default: <agent>-<task id prefix>)")
    ap.add_argument("--ca-file", help="CA bundle for the orchestrator's TLS certificate")
    ap.add_argument("--force", action="store_true", help="overwrite an existing fixture directory")
    args = ap.parse_args()

    query = urllib.parse.urlencode({"name": args.name}) if args.name else ""
    url = f"{args.url.rstrip('/')}/api/tasks/{urllib.parse.quote(args.task)}/fixture" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, headers={"X-Admin-Token": args.token})
    context = ssl.create_default_context(cafile=args.ca_file) if args.ca_file else None
    with urllib.request.urlopen(req, timeout=120, context=context) as resp:  # noqa: S310 - operator-supplied URL
        data = resp.read()
        repo_mode = resp.headers.get("X-Fixture-Repo", "?")

    fixtures = ROOT / "eval" / "fixtures"
    staging = fixtures / ".incoming"
    if staging.exists():
        import shutil
        shutil.rmtree(staging)
    name = safe_extract(data, staging)
    final = fixtures / name
    if final.exists():
        if not args.force:
            raise SystemExit(f"{final} exists; pass --force or --name")
        import shutil
        shutil.rmtree(final)
    (staging / name).rename(final)
    staging.rmdir()
    print(f"wrote {final} (repository: {repo_mode}). Edit {final / 'case.yaml'}: fill in `expect`, drop `draft: true`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
