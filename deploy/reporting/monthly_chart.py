import argparse
import os
import sqlite3
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--month", required=True)
    parser.add_argument("--chart", action="store_true")
    parser.add_argument("--report", action="store_true")
    return parser.parse_args()


def month_range(month_str: str):
    start = datetime.strptime(month_str + "-01", "%Y-%m-%d")
    if start.month == 12:
        end = datetime(start.year + 1, 1, 1)
    else:
        end = datetime(start.year, start.month + 1, 1)
    return start, end


def dedupe_rows(rows, dedupe_seconds):
    deduped = []
    last_seen = {}
    for row in rows:
        ts, person, vehicle, direction = row
        key = (person, vehicle, direction)
        ts_dt = datetime.fromisoformat(ts)
        prev = last_seen.get(key)
        if prev and (ts_dt - prev).total_seconds() <= dedupe_seconds:
            continue
        last_seen[key] = ts_dt
        deduped.append(row)
    return deduped


def report(db_path, month_str, dedupe_seconds):
    start, end = month_range(month_str)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT ts_utc, person_identity, vehicle_identity, direction
        FROM driver_attribution
        WHERE ts_utc >= ? AND ts_utc < ?
        ORDER BY ts_utc ASC
        """,
        (start.isoformat(), end.isoformat()),
    )
    rows = cursor.fetchall()
    conn.close()
    rows = dedupe_rows(rows, dedupe_seconds)

    aggregate = {}
    for ts, person, vehicle, direction in rows:
        day = ts.split("T")[0]
        key = (day, person or "unknown_person", vehicle or "unknown_vehicle")
        agg = aggregate.setdefault(key, {"in": 0, "out": 0})
        if direction in ("in", "out"):
            agg[direction] += 1

    print("day\tperson_identity\tvehicle_identity\tin_count\tout_count\ttotal")
    for (day, person, vehicle), counts in sorted(aggregate.items()):
        total = counts["in"] + counts["out"]
        print(f"{day}\t{person}\t{vehicle}\t{counts['in']}\t{counts['out']}\t{total}")


def chart(db_path, month_str, dedupe_seconds, top_pairs):
    start, end = month_range(month_str)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT ts_utc, person_identity, vehicle_identity, direction
        FROM driver_attribution
        WHERE ts_utc >= ? AND ts_utc < ?
        ORDER BY ts_utc ASC
        """,
        (start.isoformat(), end.isoformat()),
    )
    rows = cursor.fetchall()
    conn.close()
    rows = dedupe_rows(rows, dedupe_seconds)

    day_counts = {}
    pair_totals = {}
    for ts, person, vehicle, direction in rows:
        day = ts.split("T")[0]
        person = person or "unknown_person"
        vehicle = vehicle or "unknown_vehicle"
        pair = f"{person}/{vehicle}"
        pair_totals[pair] = pair_totals.get(pair, 0) + 1
        day_counts.setdefault(pair, {}).setdefault(day, 0)
        day_counts[pair][day] += 1

    top_pairs_list = sorted(pair_totals.items(), key=lambda x: x[1], reverse=True)[:top_pairs]
    top_pairs_keys = [p for p, _ in top_pairs_list]

    days = []
    current = start
    while current < end:
        days.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)

    plt.figure(figsize=(12, 6))
    for pair in top_pairs_keys:
        series = [day_counts.get(pair, {}).get(day, 0) for day in days]
        plt.plot(days, series, marker="o", label=pair)

    plt.xticks(rotation=45)
    plt.xlabel("Day")
    plt.ylabel("Trips")
    plt.title(f"Trips per day - {month_str}")
    plt.legend(loc="upper right", fontsize="small")
    plt.tight_layout()

    output_dir = os.path.join(os.path.dirname(db_path), "reports")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"trips_{month_str}.png")
    plt.savefig(output_path)
    print(output_path)


def main():
    args = parse_args()
    dedupe_seconds = int(os.getenv("DEDUPE_SECONDS", "15"))
    top_pairs = int(os.getenv("TOP_PAIRS", "10"))

    if args.report:
        report(args.db, args.month, dedupe_seconds)
    if args.chart:
        chart(args.db, args.month, dedupe_seconds, top_pairs)


if __name__ == "__main__":
    main()
