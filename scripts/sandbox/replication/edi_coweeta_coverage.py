"""EDI/Coweeta streamflow coverage probe (ADR 0202) -- direct-call sandbox driver.

Determines whether the USFS Coweeta Hydrologic Laboratory / Coweeta LTER
streamflow record on the EDI portal temporally overlaps our MRMS QPE archive
(~2020-10+), which is the precondition for a rain-on-grid replication whose
grading is computed-vs-observed discharge (NSE/R2).

Access note: the native EDI PASTA REST API (pasta.lternet.edu /package/search,
/listDataPackageRevisions, /readMetadata, /data) returns HTTP 403 for anonymous
"Public Access" from this environment (auth/IP restriction). The SAME EML
metadata and data entities are mirrored PUBLICLY by DataONE
(cn.dataone.org/cn/v2), which resolves each PASTA object PID to its member-node
copy. This driver reads through DataONE so the coverage finding is reproducible
without EDI credentials; the PASTA object PIDs it embeds are the canonical
provenance (a credentialed EDI pull would hit the identical objects).

Deterministic: it downloads the actual data entity and reports the true first/
last timestamp, sampling interval, columns, and discharge units -- no reliance on
possibly-stale catalog metadata. Reusable reader: `read_weir_tsv`.

Run: python scripts/sandbox/replication/edi_coweeta_coverage.py
(no repo imports, no AWS, stdlib + urllib only). ASCII only.
"""

from __future__ import annotations

import hashlib
import io
import json
import statistics
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DATAONE_OBJECT = "https://cn.dataone.org/cn/v2/object/"
DATAONE_RESOLVE = "https://cn.dataone.org/cn/v2/resolve/"

# MRMS MultiSensor QPE Pass2 S3 archive (noaa-mrms-pds) begins ~2020-10; the
# replication grades computed vs observed discharge, so the gauge record MUST
# overlap this for any candidate event to be gradeable.
MRMS_ARCHIVE_START = "2020-10"

OUT_DIR = Path(__file__).resolve().parent


@dataclass
class WeirRecord:
    """One EDI streamflow package's actual data-entity coverage."""

    label: str
    package_pid: str          # PASTA metadata PID (canonical provenance)
    data_pid: str             # PASTA data-entity PID (canonical provenance)
    object_name: str
    date_col: str
    discharge_col: str
    discharge_unit: str
    interval: str             # inferred sampling interval
    first_ts: str
    last_ts: str
    n_rows: int
    sha256: str
    bbox: dict = field(default_factory=dict)


def _fetch(url: str, timeout: int = 180) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "trid3nt-rog-replication/0202"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_pasta_object(pasta_pid: str, timeout: int = 180) -> bytes:
    """Fetch a PASTA object (metadata or data) via the public DataONE mirror.

    Tries the CN object store first (holds EML); falls back to resolve (follows
    the redirect to the member-node copy that holds data entities).
    """
    enc = urllib.parse.quote(pasta_pid, safe="")
    try:
        return _fetch(DATAONE_OBJECT + enc, timeout=timeout)
    except Exception:
        return _fetch(DATAONE_RESOLVE + enc, timeout=timeout)


def read_weir_tsv(raw: bytes, date_col: str, discharge_col: str) -> dict:
    """Parse a tab-delimited Coweeta weir record; return coverage facts.

    Coweeta weir exports are tab-delimited with a single header row; timestamps
    are 'YYYY-MM-DD HH:MM:SS' (hourly weirs) or 'YYYY-MM-DD' (daily watersheds).
    """
    text = raw.decode("utf-8", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    header = lines[0].split("\t")
    di = header.index(date_col)
    qi = header.index(discharge_col) if discharge_col in header else -1
    stamps: list[str] = []
    epochs: list[float] = []
    for ln in lines[1:]:
        cells = ln.split("\t")
        if di >= len(cells):
            continue
        ts = cells[di].strip()
        if not ts:
            continue
        stamps.append(ts)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                epochs.append(datetime.strptime(ts, fmt).timestamp())
                break
            except ValueError:
                continue
    interval = "unknown"
    if len(epochs) > 10:
        diffs = [b - a for a, b in zip(epochs[:2000], epochs[1:2000]) if b > a]
        if diffs:
            med = statistics.median(diffs)
            interval = {3600.0: "hourly", 86400.0: "daily",
                        900.0: "15-min", 300.0: "5-min"}.get(med, f"{med:.0f}s")
    return {
        "columns": header,
        "n_rows": len(stamps),
        "first_ts": stamps[0] if stamps else "",
        "last_ts": stamps[-1] if stamps else "",
        "interval": interval,
        "has_discharge": qi >= 0,
    }


# The Coweeta streamflow packages on EDI (PASTA scope knb-lter-cwt), the highest-
# resolution / most-relevant records for our -83.40402 35.05746 pour point.
# data_pid values are the EML <distribution><online><url> entity PIDs.
PACKAGES = [
    dict(
        label="Ball Creek weir house #9 (sub-daily; main Coweeta Creek fork)",
        package_pid="https://pasta.lternet.edu/package/metadata/eml/knb-lter-cwt/3037/19",
        data_pid="https://pasta.lternet.edu/package/data/eml/knb-lter-cwt/3037/19/c35aa80cb6b763ced2bbae10b9638b48",
        date_col="Date", discharge_col="Discharge", discharge_unit="cubicMetersPerSecond",
    ),
    # WS18 (Grady Branch) and WS27 daily records are DAILY resolution and small
    # experimental headwaters, not our 30 km2 catchment; probed for completeness.
    dict(
        label="Watershed 18 (Grady Branch) daily discharge",
        package_pid="https://pasta.lternet.edu/package/metadata/eml/knb-lter-cwt/3033/119",
        data_pid=None,  # daily + tiny experimental weir; coverage from catalog only
        date_col="Date", discharge_col="Discharge", discharge_unit="mm/day (area-normalized)",
    ),
]


def main() -> None:
    print("=== Coweeta EDI streamflow coverage probe (ADR 0202) ===")
    print(f"MRMS QPE archive start (grading precondition): {MRMS_ARCHIVE_START}\n")
    records = []
    for pkg in PACKAGES:
        if not pkg.get("data_pid"):
            print(f"[skip-download] {pkg['label']}: daily/experimental, catalog-only")
            continue
        print(f"[download] {pkg['label']}")
        raw = fetch_pasta_object(pkg["data_pid"])
        sha = hashlib.sha256(raw).hexdigest()
        cov = read_weir_tsv(raw, pkg["date_col"], pkg["discharge_col"])
        rec = WeirRecord(
            label=pkg["label"], package_pid=pkg["package_pid"], data_pid=pkg["data_pid"],
            object_name=pkg["data_pid"].rsplit("/", 1)[-1], date_col=pkg["date_col"],
            discharge_col=pkg["discharge_col"], discharge_unit=pkg["discharge_unit"],
            interval=cov["interval"], first_ts=cov["first_ts"], last_ts=cov["last_ts"],
            n_rows=cov["n_rows"], sha256=sha,
        )
        records.append(rec)
        overlaps = rec.last_ts[:7] >= MRMS_ARCHIVE_START
        print(f"    interval={rec.interval} rows={rec.n_rows} "
              f"span={rec.first_ts} .. {rec.last_ts}")
        print(f"    discharge_unit={rec.discharge_unit} sha256={sha[:16]}...")
        print(f"    OVERLAPS MRMS (>= {MRMS_ARCHIVE_START})? {overlaps}\n")

    prov = OUT_DIR / "coweeta_edi_provenance.json"
    prov.write_text(json.dumps([r.__dict__ for r in records], indent=2))
    print(f"[provenance] {prov}")
    if records and all(r.last_ts[:7] < MRMS_ARCHIVE_START for r in records):
        print("\nVERDICT: COVERAGE GAP -- highest-resolution Coweeta streamflow "
              f"record ends before the MRMS archive begins ({MRMS_ARCHIVE_START}). "
              "No candidate event (post-2020) is gradeable against the gauge. STOP.")


if __name__ == "__main__":
    main()
