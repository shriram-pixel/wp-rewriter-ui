#!/usr/bin/env python3
"""Optional command-line interface over the same core as the web UI.

    python3 rewriter.py check
    python3 rewriter.py replace "old" "new" [--scope prefix|all|posts] [--apply]
    python3 rewriter.py rewrite [--limit N] [--apply]   # default is a dry-run preview
"""

import argparse
import json
import os
import sys

import core


def load_job():
    path = os.path.join(core.HERE, "job.json")
    if not os.path.isfile(path):
        sys.exit("job.json not found next to rewriter.py")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    ap = argparse.ArgumentParser(description="Drive a live WordPress site over SSH (WP-CLI).")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("check")
    rep = sub.add_parser("replace")
    rep.add_argument("old")
    rep.add_argument("new")
    rep.add_argument("--scope", choices=["prefix", "all", "posts"], default="prefix")
    rep.add_argument("--apply", action="store_true")
    rw = sub.add_parser("rewrite")
    rw.add_argument("--limit", type=int, default=0)
    rw.add_argument("--apply", action="store_true", help="write changes (default is a preview)")

    args = ap.parse_args()
    cfg = core.get_config()

    if args.cmd == "check":
        res = core.check_connection(cfg)
        if not res.get("ok"):
            print("Connection failed:", res.get("error"))
            return 1
        print("WP-CLI:   ", res["wp_cli"])
        print("WordPress:", res["wordpress"])
        print("Prefix:   ", res["prefix"])
        return 0

    if args.cmd == "replace":
        if args.apply:
            print("APPLYING changes — make sure you have a backup.")
        else:
            print("DRY RUN — re-run with --apply to commit.")
        res = core.run_replace(cfg, [(args.old, args.new)], args.scope, args.apply)
        print(res["report"])
        return res["rc"]

    if args.cmd == "rewrite":
        job = load_job()
        rows = job.get("rows", [])
        placeholders = job.get("placeholders", {})
        ids = [int(i) for i in job.get("ids", [])]
        if not ids:
            ids = [p["id"] for p in core.list_posts(cfg, job.get("post_type", "post"), job.get("post_status", ["publish"]))]
        if args.limit:
            ids = ids[: args.limit]
        dry = not args.apply
        print("%s %d post(s)%s" % ("Previewing" if dry else "Rewriting", len(ids), " (no writes)" if dry else ""))
        for pid in ids:
            res = core.process_post(cfg, pid, rows, placeholders, dry_run=dry)
            print("%-8s #%s: %s" % (res["status"].upper(), pid, res["message"]))
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())