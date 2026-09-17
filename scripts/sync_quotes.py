#!/usr/bin/env python3
"""Sync historical quotes for stocks mentioned in Knowledge Planet topics.

Runs on the Mac host and calls the local FastAPI app in small, resumable batches.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from urllib.request import Request, urlopen


DEFAULT_APP = "http://127.0.0.1:8000"


def read_json(url: str, payload: dict | None = None) -> object:
    request = Request(url, method="POST" if payload is not None else "GET")
    if payload is not None:
        request.data = json.dumps(payload).encode()
        request.add_header("Content-Type", "application/json")
    with urlopen(request, timeout=600) as response:
        return json.loads(response.read().decode())


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"completed_codes": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def next_batch(codes: list[str], completed: set[str], size: int) -> list[str]:
    return [code for code in codes if code not in completed][:size]


def main() -> int:
    parser = argparse.ArgumentParser(description="同步主题关联股票的历史行情（支持断点续拉）")
    parser.add_argument("--app-url", default=DEFAULT_APP)
    parser.add_argument("--start-date", default=f"{date.today().year}-01-01")
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument("--batch-size", type=int, default=10, help="每次请求的股票数")
    parser.add_argument("--batches", type=int, default=20, help="本次最多处理的批次数")
    parser.add_argument("--state-file", default="data/quote-sync-state.json")
    args = parser.parse_args()
    if args.batch_size < 1 or args.batches < 1:
        parser.error("--batch-size 和 --batches 必须大于 0")

    state_path = Path(args.state_file)
    state = load_state(state_path)
    completed = set(state.get("completed_codes", []))
    codes = read_json(f"{args.app_url.rstrip('/')}/api/stocks/mentioned")
    if not isinstance(codes, list):
        raise RuntimeError("应用没有返回股票代码列表")
    imported = skipped = batches = 0
    for _ in range(args.batches):
        batch = next_batch(codes, completed, args.batch_size)
        if not batch:
            break
        result = read_json(f"{args.app_url.rstrip('/')}/api/quotes/sync", {
            "stock_codes": batch, "start_date": args.start_date, "end_date": args.end_date, "adjust_type": "qfq",
        })
        imported += result["imported"]
        skipped += result["skipped"]
        completed.update(batch)
        state["completed_codes"] = sorted(completed)
        state["start_date"], state["end_date"] = args.start_date, args.end_date
        save_state(state_path, state)
        batches += 1
        print(json.dumps({"batch": batches, "completed": len(completed), "total": len(codes),
                          "imported": imported, "skipped": skipped}, ensure_ascii=False))
    print(json.dumps({"completed": len(completed), "total": len(codes), "remaining": len(codes) - len(completed),
                      "imported": imported, "skipped": skipped, "state_file": str(state_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
