"""Verify local research evidence or prepare the original October Python runner."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "research" / "manifest.json"
ARCHIVE = ROOT / "research" / "october-source"
OCTOBER = ROOT / "evidence" / "hardening-2026-10-13" / "environment.json"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def verify():
    manifest = json.loads(INVENTORY.read_text(encoding="utf-8"))
    errors = []
    for name, expected in manifest["files"].items():
        path = ROOT / name
        try:
            digest = sha256(path.read_bytes())
        except FileNotFoundError:
            errors.append(f"Missing: {name}")
        else:
            if digest != expected:
                errors.append(f"Checksum mismatch: {name}")
    return len(manifest["files"]), errors


def prepare_october(destination):
    environment = json.loads(OCTOBER.read_text(encoding="utf-8"))
    sources = {}
    failures = []
    for name, expected in environment["source_sha256"].items():
        if not (name.startswith(("backend/", "evals/", "tests/")) or name in {
            "requirements.txt", "requirements-tested.txt", "README.md", "docs/HARDENING.md",
        }):
            continue
        current = ROOT / name if name != "README.md" else ROOT / "docs" / "RESEARCH-README.md"
        for source in (ARCHIVE / name, current):
            if source.is_file():
                data = source.read_bytes()
                if sha256(data) == expected:
                    sources[name] = data
                    break
        else:
            failures.append(name)
    if failures:
        raise RuntimeError("Cannot reconstruct original runner: " + ", ".join(failures))

    destination.mkdir(mode=0o700)
    for name, data in sources.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    requirements = environment["python_distributions"]
    (destination / "requirements-october.txt").write_text(
        "".join(f"{name}=={version}\n" for name, version in sorted(requirements.items()) if name != "pip"),
        encoding="utf-8",
    )
    return len(sources)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify")
    prepare = sub.add_parser("prepare-october")
    prepare.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.command == "verify":
        count, errors = verify()
        if errors:
            print("\n".join(errors))
            return 1
        print(f"Verified {count} frozen research files")
        return 0
    try:
        count = prepare_october(args.destination)
    except (OSError, RuntimeError) as exc:
        parser.exit(1, str(exc) + "\n")
    print(f"Prepared {count} hash-verified historical files at {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
