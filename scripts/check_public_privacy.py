"""Scan tracked public files without printing matched private values.
Run: python scripts/check_public_privacy.py
Heuristic check only; it does not replace manual review or secret scanning.
"""
from pathlib import Path
import re
import subprocess
import sys

HOME = re.compile(r"(?:/Users/|/home/|[A-Za-z]:[\\/]{1,2}Users[\\/]{1,2})([A-Za-z0-9_.-]+)")
EXAMPLES = {"example", "user", "username", "test", "tester", "alice", "bob", "runner", "developer", "dev", "your-user", "your-name", "public", "default"}
KEYS = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    "provider-key": re.compile(r"\bsk-(?:proj-|svcacct-|ant-api\d+-)?[A-Za-z0-9_-]{32,}"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})"),
    "google-api-key": re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
}

def main():
    result = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True)
    failures = []
    checked = 0
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        path = Path(raw.decode("utf-8"))
        if path.name.startswith("._") or path.name == ".DS_Store" or "__MACOSX" in path.parts:
            failures.append((str(path), "OS metadata"))
            continue
        if path.is_symlink():
            failures.append((str(path), "symlink needs review"))
            continue
        try:
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            failures.append((str(path), "unreadable/binary file needs review"))
            continue
        if "\x00" in text:
            failures.append((str(path), "binary file needs review"))
            continue
        checked += 1
        if any(m[1].lower() not in EXAMPLES for m in HOME.finditer(text)):
            failures.append((str(path), "personal home path"))
        for label, pattern in KEYS.items():
            if pattern.search(text):
                failures.append((str(path), label + " candidate"))
    for path, reason in failures:
        print(f"REVIEW: {path}: {reason}")
    print(f"Tracked text files checked: {checked}; review items: {len(failures)}")
    return 1 if failures else 0

if __name__ == "__main__":
    sys.exit(main())
