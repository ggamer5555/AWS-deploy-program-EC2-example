#!/usr/bin/env python3
"""Decode Binance collector .bin files into readable rows."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import struct
import sys
from pathlib import Path


RECORD_FORMAT = "<24sqqddddddqdddd"
RECORD_SIZE = struct.calcsize(RECORD_FORMAT)
FIELDS = [
    "symbol",
    "open_time_ms",
    "close_time_ms",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "trades",
    "taker_base_vol",
    "taker_quote_vol",
    "maker_base_vol",
    "maker_quote_vol",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Decode a 128-byte KlineRecord .bin file.")
    parser.add_argument("bin_file", type=Path)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--csv", action="store_true", help="Write CSV to stdout.")
    return parser.parse_args()


def utc_ms(value: int) -> str:
    return dt.datetime.fromtimestamp(value / 1000, tz=dt.UTC).isoformat()


def decode_record(blob: bytes) -> dict[str, object]:
    unpacked = struct.unpack(RECORD_FORMAT, blob)
    symbol = unpacked[0].split(b"\0", 1)[0].decode("ascii", errors="replace")
    row = dict(zip(FIELDS, (symbol, *unpacked[1:])))
    row["open_time_utc"] = utc_ms(int(row["open_time_ms"]))
    row["close_time_utc"] = utc_ms(int(row["close_time_ms"]))
    return row


def iter_records(path: Path):
    with path.open("rb") as file:
        while True:
            blob = file.read(RECORD_SIZE)
            if not blob:
                break
            if len(blob) != RECORD_SIZE:
                raise SystemExit(f"Partial record at end of file: {len(blob)} bytes")
            yield decode_record(blob)


def main() -> int:
    args = parse_args()
    path = args.bin_file.expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"File not found: {path}")

    size = path.stat().st_size
    if size % RECORD_SIZE != 0:
        raise SystemExit(f"File size {size} is not a multiple of {RECORD_SIZE}")

    rows = []
    for index, row in enumerate(iter_records(path)):
        if index >= args.limit:
            break
        rows.append(row)

    output_fields = ["open_time_utc", "close_time_utc", *FIELDS]
    if args.csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=output_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    else:
        print(f"{path}")
        print(f"record_size={RECORD_SIZE} records={size // RECORD_SIZE} showing={len(rows)}")
        for row in rows:
            print(
                f"{row['open_time_utc']} {row['symbol']} "
                f"O={row['open']} H={row['high']} L={row['low']} C={row['close']} "
                f"V={row['volume']} trades={row['trades']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
