"""Utility: force-generate dataset.csv for one or many sessions.

Usage examples:
  python tools/regenerate_dataset.py --session data/sessions/20260523_184630 --force
  python tools/regenerate_dataset.py --all --force
  python tools/regenerate_dataset.py --all --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable

# Ensure project root is on sys.path so importing top-level modules works when
# this script is executed from tools/ or from the project root.
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def regenerate_one(session_dir: Path, max_gap_ms: float, force: bool, dry_run: bool) -> dict | None:
    from capture_dataset import write_dataset_csv

    if not session_dir.exists() or not session_dir.is_dir():
        print(f"skip: not a dir: {session_dir}")
        return None

    dataset_path = session_dir / "dataset.csv"
    if dataset_path.exists() and not force:
        print(f"skip (exists): {dataset_path}")
        return None

    print(f"regenerating: {session_dir} (force={force} dry_run={dry_run})")
    if dry_run:
        return {"session": str(session_dir), "action": "dry-run"}

    try:
        result = write_dataset_csv(session_dir, max_gap_ms)
        print(f"wrote: {dataset_path} -> {result}")
        return {"session": str(session_dir), "result": result}
    except Exception as exc:  # pragmatic: continue on error
        print(f"error writing {dataset_path}: {exc}")
        return {"session": str(session_dir), "error": str(exc)}


def sessions_in(base: Path) -> Iterable[Path]:
    if not base.exists():
        return []
    return sorted([p for p in base.iterdir() if p.is_dir()])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Regenerate dataset.csv for sessions")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--session", help="single session dir (path)")
    group.add_argument("--all", action="store_true", help="process all sessions under data/sessions")
    parser.add_argument("--base", default="data/sessions", help="base sessions directory")
    parser.add_argument("--max-gap", type=float, default=25.0, help="max packet gap ms passed to writer")
    parser.add_argument("--force", action="store_true", help="overwrite existing dataset.csv")
    parser.add_argument("--dry-run", action="store_true", help="only show what would be done")
    args = parser.parse_args(argv)

    base = Path(args.base)
    work: list[Path] = []
    if args.session:
        work = [Path(args.session)]
    elif args.all:
        work = list(sessions_in(base))

    any_errors = False
    for session in work:
        res = regenerate_one(session, args.max_gap, args.force, args.dry_run)
        if res and "error" in res:
            any_errors = True

    return 1 if any_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
