#!/usr/bin/env python3
"""Explicit one-time custom vocabulary preparation; never run by the server."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare a local YOLO-World checkpoint with embedded object labels. May download CLIP weights only with --allow-downloads.")
    parser.add_argument("--model", required=True, type=Path, help="Existing local YOLO-World .pt checkpoint")
    parser.add_argument("--output", required=True, type=Path, help="New .pt checkpoint to create")
    parser.add_argument("--classes", required=True, nargs="+", help='Object labels, e.g. "button" "gear icon" "search icon"')
    parser.add_argument("--allow-downloads", action="store_true", help="Explicitly allow the one-time CLIP model download for encoding labels")
    args = parser.parse_args()
    if not args.allow_downloads:
        parser.error("Vocabulary encoding may download CLIP weights. Re-run with --allow-downloads to explicitly enable this preparation step.")
    model_path = args.model.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    if not model_path.is_file() or model_path.suffix.lower() != ".pt":
        parser.error("--model must be an existing local YOLO-World .pt checkpoint; this script does not download a detector.")
    if output_path.suffix.lower() != ".pt" or output_path.exists():
        parser.error("--output must be a new .pt file (existing files are never overwritten).")
    labels = list(dict.fromkeys(label.strip() for label in args.classes if label.strip()))
    if not labels or len(labels) > 100:
        parser.error("Supply 1 to 100 nonempty object labels.")
    os.environ["YOLO_AUTOINSTALL"] = "false"
    os.environ["YOLO_OFFLINE"] = "false"
    try:
        from ultralytics import YOLOWorld, settings
        import clip  # noqa: F401; require dependencies before loading large models
    except ImportError:
        parser.error("Install requirements-perception.txt and the CLIP dependency described in docs/perception.md before preparing labels.")
    settings.update({"sync": False})
    model = YOLOWorld(str(model_path), verbose=False)
    if getattr(model.model, "txt_feats", None) is None:
        parser.error("The input checkpoint must be YOLO-World, with its text embedding architecture.")
    model.set_classes(labels)
    # Inference only needs embedded labels; do not retain the text encoder.
    model.model.clip_model = None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    model.save(str(output_path))
    output_path.with_suffix(".labels.json").write_text(json.dumps({"classes": labels, "source": model_path.name}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Created {output_path}")
    print(f"Set COMPUTERUSE_YOLO_MODEL to this absolute path and restart ComputerUSE.")


if __name__ == "__main__":
    main()
