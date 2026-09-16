#!/usr/bin/env python3
"""Export a pinned, tracked-only Hermes checkout; never copy its user profile."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def prepare(repository: Path, commit: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Use an explicit full commit SHA")
    archive = subprocess.run(["git", "-C", str(repository), "archive", "--format=tar", commit],
                             check=True, capture_output=True).stdout
    destination = ROOT / ".tools" / "hermes" / commit
    entries = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        members = bundle.getmembers()
        for member in members:
            relative = Path(member.name)
            if relative.is_absolute() or ".." in relative.parts or member.name == ".env":
                raise ValueError("Unexpected path in tracked source export")
            if not (member.isfile() or member.isdir() or member.issym()):
                raise ValueError("Unsupported source archive entry")
            if member.issym():
                resolved = (destination / relative.parent / member.linkname).resolve()
                if not resolved.is_relative_to(destination.resolve()):
                    raise ValueError("Source symlink leaves the isolated export")
                entries[member.name] = {"symlink": member.linkname}
            elif member.isfile():
                entries[member.name] = {"sha256": hashlib.sha256(bundle.extractfile(member).read()).hexdigest()}
        if destination.exists():
            for name, identity in entries.items():
                path = destination / name
                actual = {"symlink": path.readlink().as_posix()} if path.is_symlink() else {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                if actual != identity:
                    raise ValueError("Existing export differs from the pinned tracked source; it was not overwritten")
        else:
            destination.mkdir(parents=True)
            bundle.extractall(destination, filter="data")
    tree_digest = hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    manifest = {"source_repository": str(repository), "commit": commit,
                "archive_sha256": hashlib.sha256(archive).hexdigest(),
                "source_path": str(destination), "entries": len(members),
                "tree_sha256": tree_digest, "files": entries,
                "user_profile_copied": False, "source_modified": False}
    (destination.parent / f"{commit}.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    manifest = prepare(args.repository, args.commit)
    print(json.dumps({key: value for key, value in manifest.items() if key != "files"}, indent=2))
