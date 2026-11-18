#!/usr/bin/env python3
"""
Taipei YouBike 2.0 / 2.0E Shared-Dock Snapshot Collector (Pure Python Stdlib)

Captures official real-time Taipei City (area_code == "00") station dock states
splitting regular bikes (yb2 / 2.0) and electric-assist bikes (eyb / 2.0E), plus
empty and forbidden docks.

Modes:
  --once           Fetch 1 snapshot immediately, save, and exit (~3 seconds).
  --resident-hour  Hourly resident batch mode: capture immediately, then wake at
                   every 10-minute clock mark (:10, :20, :30, :40, :50 + 5s)
                   until :54 of the current hour so all 6 snapshots are pushed
                   in a single hourly Git commit before the next hour's job.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

API_URL = "https://apis.youbike.com.tw/json/station-yb2.json"
TAIPEI_AREA_CODE = "00"
TZ_TAIPEI = ZoneInfo("Asia/Taipei")

META_FIELDS = [
    "station_no",
    "name_tw",
    "district_tw",
    "address_tw",
    "lat",
    "lng",
    "latest_capacity",
    "first_seen_date",
    "last_seen_date",
]

SNAPSHOT_FIELDS = [
    "slot_time",
    "fetched_at",
    "src_updated_at",
    "station_no",
    "status",
    "capacity",
    "yb2",
    "eyb",
    "empty",
    "forbidden",
]


def now_taipei() -> datetime:
    return datetime.now(TZ_TAIPEI)


def floor_slot(dt: datetime, interval_minutes: int) -> datetime:
    floored_min = (dt.minute // interval_minutes) * interval_minutes
    return dt.replace(minute=floored_min, second=0, microsecond=0)


def norm_station_name(name: str) -> str:
    s = (name or "").strip()
    if s.startswith("YouBike2.0_"):
        s = s[len("YouBike2.0_") :]
    return s.replace("（", "(").replace("）", ")").replace("　", " ").strip()


def fetch_taipei_stations(retries: int = 3, timeout: int = 25) -> list[dict]:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; TaipeiYouBikeResearchCollector/1.0)",
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
    }
    backoff_seconds = [2, 5, 12]
    last_err: Exception | None = None

    for attempt in range(retries):
        try:
            req = urllib.request.Request(API_URL, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_bytes = resp.read()
                if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                    raw_bytes = gzip.decompress(raw_bytes)
            payload = json.loads(raw_bytes.decode("utf-8"))
            if not isinstance(payload, list):
                raise ValueError(f"Unexpected JSON root type: {type(payload).__name__}")

            taipei = [
                row
                for row in payload
                if str(row.get("area_code", "")).strip() == TAIPEI_AREA_CODE
                and str(row.get("station_no", "")).strip()
            ]
            if len(taipei) < 1000:
                raise ValueError(f"Taipei station count abnormally low ({len(taipei)} < 1000)")
            return taipei
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError, OSError) as exc:
            last_err = exc
            if attempt < retries - 1:
                wait_s = backoff_seconds[min(attempt, len(backoff_seconds) - 1)]
                print(
                    f"[WARN] Fetch attempt {attempt + 1}/{retries} failed ({exc}); retrying in {wait_s}s...",
                    flush=True,
                )
                time.sleep(wait_s)

    raise RuntimeError(f"Failed to fetch Taipei YouBike stations after {retries} attempts: {last_err}")


def upsert_stations_meta(meta_path: Path, taipei_rows: list[dict], today_str: str) -> int:
    existing: dict[str, dict[str, str]] = {}
    if meta_path.exists():
        with meta_path.open("r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                sno = row.get("station_no", "").strip()
                if sno:
                    existing[sno] = row

    changed = not meta_path.exists()
    for s in taipei_rows:
        sno = str(s["station_no"]).strip()
        name_clean = norm_station_name(str(s.get("name_tw", "")))
        district = str(s.get("district_tw", "")).strip()
        address = str(s.get("address_tw", "")).strip()
        lat = str(s.get("lat", "")).strip()
        lng = str(s.get("lng", "")).strip()
        cap = str(int(s.get("parking_spaces") or 0))

        prev = existing.get(sno)
        if prev is None:
            existing[sno] = {
                "station_no": sno,
                "name_tw": name_clean,
                "district_tw": district,
                "address_tw": address,
                "lat": lat,
                "lng": lng,
                "latest_capacity": cap,
                "first_seen_date": today_str,
                "last_seen_date": today_str,
            }
            changed = True
        else:
            new_rec = {
                "station_no": sno,
                "name_tw": name_clean or prev.get("name_tw", ""),
                "district_tw": district or prev.get("district_tw", ""),
                "address_tw": address or prev.get("address_tw", ""),
                "lat": lat or prev.get("lat", ""),
                "lng": lng or prev.get("lng", ""),
                "latest_capacity": cap,
                "first_seen_date": prev.get("first_seen_date") or today_str,
                "last_seen_date": today_str,
            }
            if new_rec != prev:
                existing[sno] = new_rec
                changed = True

    if changed:
        tmp_path = meta_path.with_suffix(".csv.tmp")
        with tmp_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=META_FIELDS)
            writer.writeheader()
            for sno in sorted(existing):
                writer.writerow(existing[sno])
        os.replace(tmp_path, meta_path)

    return len(existing)


def upsert_daily_snapshot(
    daily_gz_path: Path,
    new_rows: list[dict[str, str | int]],
) -> tuple[int, int]:
    """
    Merge new snapshot rows into daily .csv.gz keyed by (slot_time, station_no),
    writing a single clean gzip stream atomically so pandas / Excel / gzip never
    see duplicate rows or repeated mid-file headers.
    """
    records_by_key: dict[tuple[str, str], dict[str, str | int]] = {}

    if daily_gz_path.exists():
        with gzip.open(daily_gz_path, "rt", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                key = (row["slot_time"], row["station_no"])
                records_by_key[key] = row

    for row in new_rows:
        key = (str(row["slot_time"]), str(row["station_no"]))
        records_by_key[key] = row

    sorted_keys = sorted(records_by_key.keys())
    unique_slots = {k[0] for k in sorted_keys}

    tmp_gz = daily_gz_path.with_suffix(".gz.tmp")
    with gzip.open(tmp_gz, "wt", encoding="utf-8", newline="", compresslevel=6) as gz_out:
        writer = csv.DictWriter(gz_out, fieldnames=SNAPSHOT_FIELDS)
        writer.writeheader()
        for k in sorted_keys:
            writer.writerow(records_by_key[k])
    os.replace(tmp_gz, daily_gz_path)

    return len(unique_slots), len(sorted_keys)


def capture_once(data_dir: Path, interval_minutes: int) -> dict:
    fetched_dt = now_taipei()
    slot_dt = floor_slot(fetched_dt, interval_minutes)
    date_str = slot_dt.strftime("%Y-%m-%d")
    slot_str = slot_dt.strftime("%Y-%m-%d %H:%M:%S")
    fetched_str = fetched_dt.strftime("%Y-%m-%d %H:%M:%S")

    taipei_rows = fetch_taipei_stations()

    snapshots_dir = data_dir / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    meta_path = data_dir / "stations_meta.csv"
    daily_gz_path = snapshots_dir / f"{date_str}.csv.gz"
    status_path = data_dir / "collection_status.json"

    total_known_stations = upsert_stations_meta(meta_path, taipei_rows, date_str)

    parsed_rows: list[dict[str, str | int]] = []
    active_count = 0
    sum_yb2 = 0
    sum_eyb = 0
    sum_empty = 0
    false_green_count = 0

    for s in sorted(taipei_rows, key=lambda x: str(x["station_no"])):
        sno = str(s["station_no"]).strip()
        st_status = int(s.get("status") or 0)
        cap = int(s.get("parking_spaces") or 0)
        detail = s.get("available_spaces_detail") or {}
        yb2 = int(detail.get("yb2") or 0)
        eyb = int(detail.get("eyb") or 0)
        avail_total = int(s.get("available_spaces") if s.get("available_spaces") is not None else (yb2 + eyb))
        empty = int(s.get("empty_spaces") if s.get("empty_spaces") is not None else max(0, cap - avail_total))
        forbidden = int(s.get("forbidden_spaces") or 0)
        src_updated = str(s.get("updated_at") or s.get("time") or "").strip()

        parsed_rows.append(
            {
                "slot_time": slot_str,
                "fetched_at": fetched_str,
                "src_updated_at": src_updated,
                "station_no": sno,
                "status": st_status,
                "capacity": cap,
                "yb2": yb2,
                "eyb": eyb,
                "empty": empty,
                "forbidden": forbidden,
            }
        )

        if st_status == 1:
            active_count += 1
            sum_yb2 += yb2
            sum_eyb += eyb
            sum_empty += empty
            if (yb2 + eyb) >= 5 and eyb == 0:
                false_green_count += 1

    today_slots, today_rows = upsert_daily_snapshot(daily_gz_path, parsed_rows)
    gz_size_kb = round(daily_gz_path.stat().st_size / 1024.0, 1)

    summary = {
        "last_slot_time_taipei": slot_str,
        "last_fetched_at_taipei": fetched_str,
        "latest_daily_file": f"data/snapshots/{date_str}.csv.gz",
        "latest_daily_file_size_kb": gz_size_kb,
        "today_captured_slots": today_slots,
        "today_total_rows": today_rows,
        "taipei_stations_in_snapshot": len(parsed_rows),
        "taipei_active_stations": active_count,
        "known_stations_in_meta": total_known_stations,
        "live_parked_yb2": sum_yb2,
        "live_parked_eyb": sum_eyb,
        "live_empty_docks": sum_empty,
        "false_green_ge5_total_0_eyb_stations": false_green_count,
    }
    tmp_status = status_path.with_suffix(".json.tmp")
    tmp_status.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_status, status_path)

    print(
        f"[OK] slot={slot_str} | fetched={fetched_str} | stations={len(parsed_rows)} "
        f"(active={active_count}) | yb2={sum_yb2:,} | eyb={sum_eyb:,} | "
        f"today_slots={today_slots} ({gz_size_kb} KB)",
        flush=True,
    )
    return summary


def run_resident_hour(data_dir: Path, interval_minutes: int, stop_minute: int) -> int:
    start_dt = now_taipei()
    # Handle slight early runner clock skew (e.g., 19:59:45 for the 20:00 cron)
    if start_dt.minute == 59 and start_dt.second >= 30:
        wait_align = (60 - start_dt.second) + 5
        print(f"[INFO] Started slightly before the hour ({start_dt:%H:%M:%S}); aligning +{wait_align}s...", flush=True)
        time.sleep(wait_align)
        start_dt = now_taipei()

    cutoff_dt = start_dt.replace(minute=stop_minute, second=0, microsecond=0)
    print(
        f"[START] Hourly resident batch started at {start_dt:%Y-%m-%d %H:%M:%S %Z}; "
        f"interval={interval_minutes}m, cutoff={cutoff_dt:%H:%M:%S}",
        flush=True,
    )

    success_count = 0
    while True:
        try:
            capture_once(data_dir=data_dir, interval_minutes=interval_minutes)
            success_count += 1
        except Exception as exc:
            print(f"[ERROR] Snapshot tick failed: {exc}", file=sys.stderr, flush=True)

        cur_dt = now_taipei()
        next_slot = floor_slot(cur_dt, interval_minutes) + timedelta(minutes=interval_minutes)
        # Wake 5 seconds past the 10-minute mark so official minute-level data has refreshed
        wake_dt = next_slot + timedelta(seconds=5)

        if wake_dt > cutoff_dt:
            print(
                f"[DONE] Next tick ({wake_dt:%H:%M:%S}) is past hourly cutoff ({cutoff_dt:%H:%M:%S}). "
                f"Completed {success_count} snapshot(s) in this batch.",
                flush=True,
            )
            break

        sleep_sec = max(1.0, (wake_dt - cur_dt).total_seconds())
        print(f"[WAIT] Sleeping {sleep_sec:.1f}s until next tick at {wake_dt:%Y-%m-%d %H:%M:%S}...", flush=True)
        time.sleep(sleep_sec)

    return 0 if success_count > 0 else 1


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Taipei YouBike 2.0/2.0E Dock Snapshot Collector")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--once",
        action="store_true",
        help="Fetch a single snapshot immediately and exit (ideal for manual quick verification).",
    )
    mode.add_argument(
        "--resident-hour",
        action="store_true",
        help="Run resident loop within the current hour, capturing every --interval-minutes until --stop-minute.",
    )
    p.add_argument(
        "--interval-minutes",
        type=int,
        default=10,
        choices=[5, 10, 15, 20, 30],
        help="Snapshot slot interval in minutes (default: 10).",
    )
    p.add_argument(
        "--stop-minute",
        type=int,
        default=54,
        help="Minute of the hour (0-58) after which resident loop exits to push before next hour (default: 54).",
    )
    p.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "data",
        help="Output directory for stations_meta.csv and snapshots/YYYY-MM-DD.csv.gz (default: ./data).",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.resident_hour:
        return run_resident_hour(
            data_dir=args.data_dir,
            interval_minutes=args.interval_minutes,
            stop_minute=args.stop_minute,
        )
    capture_once(data_dir=args.data_dir, interval_minutes=args.interval_minutes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
