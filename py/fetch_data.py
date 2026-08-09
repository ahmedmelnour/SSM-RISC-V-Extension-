#!/usr/bin/env python3
"""
fetch_data.py -- download the MIT-BIH Arrhythmia Database, resumably.

    py/venv/bin/python py/fetch_data.py [--jobs N]

Why this exists instead of a one-line `wfdb.dl_database('mitdb', ...)`:

* **It resumes.** `dl_database` fetches the whole database in one call and leaves
  nothing usable behind if it is interrupted. Re-running this costs only what is
  still missing.
* **It has timeouts.** PhysioNet stalls mid-transfer often enough to matter, and
  `dl_database` will sit on a dead socket indefinitely -- observed hanging for
  8+ minutes on one record with no output.
* **It downloads in parallel.** PhysioNet throttles hard per connection (~13 KB/s
  measured), so a serial fetch of ~100 MB takes over an hour. Throughput scales
  with concurrent connections, so the whole database takes a few minutes instead.

A record counts as present only when all three of .dat/.hea/.atr exist, so a
partially written record is re-fetched rather than silently used. Each file is
written to a .part and renamed only on success, so an interrupted transfer can
never leave a truncated file that looks complete.
"""

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data", "mitdb")
BASE = "https://physionet.org/files/mitdb/1.0.0"

# The 48 MIT-BIH records. 102/104/107/217 are the paced records; they are
# fetched for completeness but excluded from the dataset by prep_data.py,
# because AAMI EC57 requires paced beats to be omitted from reported results.
RECORDS = [
    "100", "101", "102", "103", "104", "105", "106", "107", "108", "109",
    "111", "112", "113", "114", "115", "116", "117", "118", "119", "121",
    "122", "123", "124", "200", "201", "202", "203", "205", "207", "208",
    "209", "210", "212", "213", "214", "215", "217", "219", "220", "221",
    "222", "223", "228", "230", "231", "232", "233", "234",
]

EXTS = (".dat", ".hea", ".atr")
RETRIES = 4
CONNECT_TIMEOUT = 15
READ_TIMEOUT = 60          # per socket read, not per file -- a stall trips this


def fetch_one(name):
    """Download a single file to DATA_DIR. Returns (name, ok, message)."""
    dest = os.path.join(DATA_DIR, name)
    if os.path.exists(dest):
        return (name, True, "present")

    part = dest + ".part"
    url = "%s/%s" % (BASE, name)

    for attempt in range(1, RETRIES + 1):
        try:
            with requests.get(url, stream=True,
                              timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)) as r:
                r.raise_for_status()
                with open(part, "wb") as f:
                    for chunk in r.iter_content(65536):
                        f.write(chunk)
            os.replace(part, dest)          # atomic: no truncated file survives
            return (name, True, "ok")
        except Exception as exc:            # noqa: BLE001
            if os.path.exists(part):
                os.remove(part)
            if attempt == RETRIES:
                return (name, False, str(exc))
    return (name, False, "unreachable")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=12,
                    help="parallel downloads (default 12)")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)

    wanted = ["%s%s" % (r, e) for r in RECORDS for e in EXTS]
    missing = [n for n in wanted
               if not os.path.exists(os.path.join(DATA_DIR, n))]

    print("[fetch] %d/%d files present, %d to fetch, %d at a time"
          % (len(wanted) - len(missing), len(wanted), len(missing), args.jobs))
    if not missing:
        print("[fetch] nothing to do")
        return 0

    done, failed = 0, []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for name, ok, msg in pool.map(fetch_one, missing):
            done += 1
            if ok:
                print("[fetch] %3d/%d  %s" % (done, len(missing), name))
            else:
                print("[fetch] %3d/%d  %s FAILED: %s"
                      % (done, len(missing), name, msg))
                failed.append(name)
            sys.stdout.flush()

    complete = [r for r in RECORDS
                if all(os.path.exists(os.path.join(DATA_DIR, r + e))
                       for e in EXTS)]
    print("[fetch] %d/%d records complete in %s"
          % (len(complete), len(RECORDS), DATA_DIR))
    if failed:
        print("[fetch] %d file(s) failed -- re-run to retry just those"
              % len(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
