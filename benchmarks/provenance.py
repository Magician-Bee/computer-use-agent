"""Reproduction metadata without environment variables or user credentials."""
from __future__ import annotations

import hashlib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import platform


def provenance(root: Path) -> dict:
    sources = {}
    for folder, pattern in (("server", "**/*.py"), ("benchmarks", "*.py"), ("benchmarks/fixtures", "*"), ("scripts", "benchmark_*.py"), ("scripts", "hermes_worker.py")):
        for file in sorted((root / folder).glob(pattern)):
            if file.is_file():
                sources[str(file.relative_to(root))] = hashlib.sha256(file.read_bytes()).hexdigest()
    packages = {}
    for name in ("pydantic", "playwright", "httpx", "Pillow", "pyautogui"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    return {"python": platform.python_version(), "system": platform.system(), "machine": platform.machine(),
            "packages": packages, "source_sha256": sources}
