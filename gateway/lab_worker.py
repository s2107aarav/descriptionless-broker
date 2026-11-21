"""Internal recovery-lab child. The operational server never uses this entry point."""
import argparse
import json
import os
from pathlib import Path

from .config import load_policy, load_registry
from .engine import Gateway
from .store import Store, runtime_lock
from .workspace import Workspace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", type=Path)
    parser.add_argument("boundary")
    args = parser.parse_args()
    root = args.runtime.absolute()
    if not root.name.startswith("broker-lab-") or not (root / ".disposable-lab").is_file():
        raise SystemExit("Recovery child requires a disposable lab runtime")

    def stop(boundary, row):
        if boundary == args.boundary:
            print(json.dumps({"event": "boundary_reached", "boundary": boundary,
                              "execution_id": row["id"], "attempt": row["attempt"]}), flush=True)
            os._exit(71)

    with runtime_lock(root), Workspace(root) as workspace:
        Gateway(Store(root), load_policy(root), load_registry(root), workspace, fault=stop).run_once()


if __name__ == "__main__":
    main()
