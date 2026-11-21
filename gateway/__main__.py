"""Offline provisioning and maintenance for the loopback gateway."""
import argparse
import json
import sys
import time

from .config import Settings, ensure_private_directory, load_policy, load_registry
from .contracts import strict_json
from .store import Store, purge_receipts, runtime_lock, verify_audit
from .workspace import Workspace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Initialize the private runtime and demo principals")
    commands.add_parser("migrate", help="Apply and validate the gateway schema")
    commands.add_parser("verify-audit", help="Check lifecycle state and audit event consistency")
    unknown = commands.add_parser("inspect-unknown", help="Show saved publication evidence for an uncertain write")
    unknown.add_argument("execution_id")
    rotate = commands.add_parser("rotate-token", help="Issue a replacement credential for a local principal")
    rotate.add_argument("principal", choices=("requester", "agent", "reviewer"))
    revoke = commands.add_parser("revoke", help="Disable a local principal")
    revoke.add_argument("principal", choices=("requester", "agent", "reviewer"))
    prune = commands.add_parser("prune", help="Remove expired transient requests and receipts")
    prune.add_argument("--days", type=int, choices=range(1, 366), metavar="1..365", default=30)
    args = parser.parse_args()

    settings = Settings.from_env()
    if args.command == "init":
        settings.root.mkdir(parents=True, mode=0o700, exist_ok=True)
    ensure_private_directory(settings.root)

    with runtime_lock(settings.root):
        store = Store(settings.root)
        store.migrate(create=args.command == "init")
        if args.command == "init":
            Workspace(settings.root).initialize()
            load_policy(settings.root)
            load_registry(settings.root)
            tokens = store.provision_demo()
            print(json.dumps({"new_credentials": tokens, "runtime": str(settings.root)}, indent=2))
        elif args.command == "rotate-token":
            print(store.rotate_token(args.principal))
        elif args.command == "revoke":
            store.revoke(args.principal)
            print("Principal disabled")
        elif args.command == "prune":
            print(json.dumps({"rows_pruned": purge_receipts(store, time.time() - args.days * 86400)}))
        elif args.command == "verify-audit":
            print(json.dumps(verify_audit(store)))
        elif args.command == "inspect-unknown":
            with store.connect() as db:
                row = db.execute("""SELECT id,reason,updated_at,stage_json FROM executions
                    WHERE id=? AND status='outcome_unknown'""", (args.execution_id,)).fetchone()
            if row is None:
                raise ValueError("No unknown execution with this ID")
            evidence = dict(row)
            raw = evidence.pop("stage_json")
            evidence["staging_checkpoint"] = strict_json(raw) if raw is not None else None
            print(json.dumps(evidence, indent=2))
        else:
            print("Gateway schema and database integrity verified")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Gateway maintenance failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
