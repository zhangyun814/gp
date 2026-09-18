#!/usr/bin/env python3
"""Sync historical quotes for stocks mentioned in Knowledge Planet topics.

Runs on the Mac host and calls the local FastAPI app in small, resumable batches.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_APP = "http://127.0.0.1:8000"


def read_json(url: str, payload: dict | None = None, max_attempts: int = 3) -> object:
    for attempt in range(1, max_attempts + 1):
        request = Request(url, method="POST" if payload is not None else "GET")
        if payload is not None:
            request.data = json.dumps(payload).encode()
            request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=600) as response:
                return json.loads(response.read().decode())
        except HTTPError as exc:
            if exc.code < 500 or attempt == max_attempts:
                raise
        except (URLError, TimeoutError):
            if attempt == max_attempts:
                raise
        time.sleep(2 ** (attempt - 1))
    raise RuntimeError("请求重试次数已耗尽")


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"completed_codes": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def next_batch(codes: list[str], completed: set[str], size: int,
               excluded: set[str] | None = None) -> list[str]:
    excluded = excluded or set()
    return [code for code in codes if code not in completed and code not in excluded][:size]


def state_for_run(state: dict, start_date: str, end_date: str, all_stocks: bool) -> dict:
    expected = (start_date, end_date, all_stocks)
    actual = (state.get("start_date"), state.get("end_date"), state.get("all_stocks"))
    return state if actual == expected else {"completed_codes": []}


def main() -> int:
    parser = argparse.ArgumentParser(description="同步主题关联股票的历史行情（支持断点续拉）")
    parser.add_argument("--app-url", default=DEFAULT_APP)
    parser.add_argument("--start-date", default=f"{date.today().year}-01-01")
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument("--batch-size", type=int, default=10, help="每次请求的股票数")
    parser.add_argument("--batches", type=int, default=20, help="本次最多处理的批次数")
    parser.add_argument("--max-attempts", type=int, default=3, help="单只股票或 HTTP 失败的最多尝试次数")
    parser.add_argument("--state-file", default="data/quote-sync-state.json")
    parser.add_argument("--all-stocks", action="store_true", help="同步当前全部上市 A 股，而不只同步主题提及股票")
    args = parser.parse_args()
    if args.batch_size < 1 or args.batches < 1 or not 1 <= args.max_attempts <= 5:
        parser.error("--batch-size、--batches 必须大于 0，--max-attempts 必须在 1 到 5 之间")

    start_date, end_date = date.fromisoformat(args.start_date), date.fromisoformat(args.end_date)
    if start_date > end_date:
        parser.error("--start-date 不能晚于 --end-date")
    if end_date > date.today():
        parser.error("--end-date 不能晚于今天，未来行情尚未产生")

    state_path = Path(args.state_file)
    state = state_for_run(load_state(state_path), args.start_date, args.end_date, args.all_stocks)
    completed = set(state.get("completed_codes", []))
    if args.all_stocks:
        master = read_json(f"{args.app_url.rstrip('/')}/api/stocks/sync-master", {})
        codes = master.get("codes") if isinstance(master, dict) else None
    else:
        codes = read_json(f"{args.app_url.rstrip('/')}/api/stocks/mentioned")
    if not isinstance(codes, list):
        raise RuntimeError("应用没有返回股票代码列表")
    state["total"] = len(codes)
    state["status"] = "running"
    state["started_at"] = datetime.now(timezone.utc).isoformat()
    state["analysis_status"] = "pending"
    save_state(state_path, state)
    imported = skipped = no_data = invalid_rows = batches = 0
    failed_codes = set(state.get("last_failed_codes") or []) - completed
    failure_reasons = dict(state.get("failure_reasons") or {})
    attempted: set[str] = set()
    for _ in range(args.batches):
        batch = next_batch(codes, completed, args.batch_size, attempted)
        if not batch:
            break
        attempted.update(batch)
        result = read_json(f"{args.app_url.rstrip('/')}/api/quotes/sync", {
            "stock_codes": batch, "start_date": args.start_date, "end_date": args.end_date, "adjust_type": "qfq",
            "max_attempts": args.max_attempts,
        }, max_attempts=args.max_attempts)
        imported += result["imported"]
        skipped += result["skipped"]
        no_data += result.get("no_data", 0)
        invalid_rows += result.get("invalid_rows", 0)
        batch_failed = set(result.get("failed_codes", []))
        succeeded = set(batch) - batch_failed
        failed_codes.difference_update(succeeded)
        failed_codes.update(batch_failed)
        completed.update(succeeded)
        for code in succeeded:
            failure_reasons.pop(code, None)
        failure_reasons.update(result.get("failure_reasons") or {})
        state["completed_codes"] = sorted(completed)
        state["start_date"], state["end_date"] = args.start_date, args.end_date
        state["all_stocks"] = args.all_stocks
        state["last_failed_codes"] = sorted(failed_codes)
        state["failure_reasons"] = failure_reasons
        save_state(state_path, state)
        batches += 1
        print(json.dumps({"batch": batches, "completed": len(completed), "total": len(codes),
                          "imported": imported, "skipped": skipped, "no_data": no_data,
                          "invalid_rows": invalid_rows, "failed": len(failed_codes)}, ensure_ascii=False))
    unattempted = set(codes) - completed - failed_codes
    if not unattempted:
        state["status"] = "analyzing"
        state["analysis_status"] = "running"
        save_state(state_path, state)
        try:
            analysis = read_json(f"{args.app_url.rstrip('/')}/api/analyze/rebuild", {},
                                 max_attempts=args.max_attempts)
        except Exception:
            state["status"] = "failed"
            state["analysis_status"] = "failed"
            save_state(state_path, state)
            raise
        state["status"] = "completed_with_failures" if failed_codes else "completed"
        state["analysis_status"] = "completed"
        state["analysis_completed_at"] = datetime.now(timezone.utc).isoformat()
        state["analysis_event_returns"] = analysis.get("event_returns", 0) if isinstance(analysis, dict) else 0
        state["finished_at"] = datetime.now(timezone.utc).isoformat()
    else:
        state["status"] = "paused"
    save_state(state_path, state)
    print(json.dumps({"completed": len(completed), "total": len(codes), "remaining": len(unattempted),
                      "imported": imported, "skipped": skipped, "no_data": no_data,
                      "invalid_rows": invalid_rows, "failed": len(failed_codes),
                      "failed_codes": sorted(failed_codes),
                      "status": state["status"], "analysis_status": state["analysis_status"],
                      "state_file": str(state_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
