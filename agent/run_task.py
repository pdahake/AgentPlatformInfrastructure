#!/usr/bin/env python3
"""
CLI that exercises the full stack end-to-end: sends a task to the running
`agent` service, which calls an LLM (via litellm) in a tool-calling loop
against Postgres-backed market/news data, and prints the result.

Usage (after `docker compose up`):
    docker compose run --rm agent python run_task.py \\
        --task "Summarize news about oil and airlines in early 2015, and relate that to how AAL traded that period" \\
        --date-from 2015-01-01 --date-to 2015-03-31
"""
import argparse
import json
import os
import sys

import httpx

AGENT_URL = os.environ.get("AGENT_URL", "http://agent:8000")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--task", required=True, help="Natural-language task for the agent")
    parser.add_argument("--date-from", dest="date_from", default=None, help="YYYY-MM-DD, inclusive")
    parser.add_argument("--date-to", dest="date_to", default=None, help="YYYY-MM-DD, inclusive")
    parser.add_argument("--model", default=None, help="Override the litellm model alias (default: server's AGENT_MODEL)")
    args = parser.parse_args()

    payload = {"task": args.task, "date_from": args.date_from, "date_to": args.date_to, "model": args.model}

    print(f"--> POST {AGENT_URL}/run", file=sys.stderr)
    try:
        resp = httpx.post(f"{AGENT_URL}/run", json=payload, timeout=180)
    except httpx.ConnectError as e:
        print(f"could not reach agent service at {AGENT_URL}: {e}", file=sys.stderr)
        print("is `docker compose up` running?", file=sys.stderr)
        sys.exit(1)

    if resp.status_code != 200:
        print(f"agent returned {resp.status_code}: {resp.text}", file=sys.stderr)
        sys.exit(1)

    result = resp.json()
    print(json.dumps(result, indent=2))
    print(f"\nTrace: http://localhost:16686/trace/{result['trace_id']}", file=sys.stderr)


if __name__ == "__main__":
    main()
