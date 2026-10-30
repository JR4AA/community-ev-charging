"""Operator commands, run inside the container on the VM (never over the tunnel):

    docker compose exec app python -m evcharge.cli list
    docker compose exec app python -m evcharge.cli show SESSION
    docker compose exec app python -m evcharge.cli settle SESSION WATT_HOURS
    docker compose exec app python -m evcharge.cli cancel SESSION [REASON]
    docker compose exec app python -m evcharge.cli solar on|off|status

Until the OCPP side exists, `settle` stands in for the charger's StopTransaction.
"""
from __future__ import annotations

import argparse
import sys

from . import config
from .db import DB
from .payments import PaymentError, Payments
from .paypal import PayPal

COLS = ("id", "connector", "status", "energy_wh", "amount_charged", "created_at", "error")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="evcharge.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("show").add_argument("session")
    st = sub.add_parser("settle")
    st.add_argument("session")
    st.add_argument("energy_wh", type=int)
    c = sub.add_parser("cancel")
    c.add_argument("session")
    c.add_argument("reason", nargs="?", default="cancelled by operator")
    sub.add_parser("solar").add_argument("state", choices=("on", "off", "status"))
    args = p.parse_args(argv)

    settings = config.from_env()
    db = DB(settings.db_path)
    payments = Payments(settings, db, PayPal(settings))
    try:
        if args.cmd == "list":
            for s in db.recent_sessions():
                print("  ".join(str(s[k] if s[k] is not None else "-") for k in COLS))
        elif args.cmd == "show":
            s = db.session(args.session)
            if not s:
                print("no such session", file=sys.stderr)
                return 1
            for k, v in s.items():
                print(f"{k:24} {v}")
            for e in db.events(args.session):
                print(f"  {e['at']}  {e['kind']:16} {e['detail']}")
        elif args.cmd == "settle":
            s = payments.settle(args.session, args.energy_wh)
            print(s["status"], s["amount_charged"])
        elif args.cmd == "solar":
            if args.state != "status":
                db.set_solar(args.state == "on")
            print("solar rate", "on" if db.solar_active() else "off")
        elif args.cmd == "cancel":
            print(payments.cancel(args.session, args.reason)["status"])
    except PaymentError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
