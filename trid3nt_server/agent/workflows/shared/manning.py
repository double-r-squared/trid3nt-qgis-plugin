"""Shared NLCD land-cover class -> Manning's n roughness table.

The version-pinned ``manning_mapping.csv`` (NLCD class integer -> Manning's n)
plus its loader. Hoisted to ``shared/`` because it is a CROSS-ENGINE substrate:
SFINCS builds its overland-roughness grid from it AND the SWMM mesh builder reads
the SAME table for its overland-conduit roughness. Neither engine should reach
into the other's package for it, so it lives here.

The CSV data lives at ``shared/data/manning_mapping.csv``. The loader raises a
typed :class:`ManningMappingError` (``error_code`` + ``details``); the SFINCS
builder translates it to its own ``SFINCSSetupError("MANNING_MAPPING_LOAD_FAILED")``
so the SFINCS failed-envelope contract stays byte-identical.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

logger = logging.getLogger("trid3nt_server.agent.workflows.shared.manning")

__all__ = [
    "ManningMappingError",
    "MANNING_MAPPING_PATH",
    "MANNING_MAPPING_VERSION",
    "load_manning_mapping",
]


class ManningMappingError(RuntimeError):
    """Raised when the NLCD -> Manning's n table cannot be loaded.

    Carries an A.6 open-set ``error_code`` (always ``MANNING_MAPPING_LOAD_FAILED``)
    and a ``details`` dict so an engine can lift it into its own typed failure.
    """

    def __init__(self, error_code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = details or {}


#: The version-pinned NLCD -> Manning's n table shipped alongside this module.
#: The validation gate reads this file once per ``build_sfincs_model`` call (no
#: module-level caching -- keep the read explicit so a hot-swap is trivial in
#: tests).
MANNING_MAPPING_PATH: Path = Path(__file__).parent / "data" / "manning_mapping.csv"

#: Version string embedded in the CSV's header block. Surfaced in
#: ``ModelSetup.parameters`` for provenance.
MANNING_MAPPING_VERSION: str = "1.0.0"


def load_manning_mapping(
    csv_path: Path | str | None = None,
) -> dict[int, float]:
    """Load the version-pinned NLCD class -> Manning's n mapping.

    Reads ``manning_mapping.csv`` (default: the shared data file) and returns
    a dict keyed by NLCD class integer. Comments (``#``) and empty lines are
    ignored; the CSV header row is consumed; data rows must have exactly two
    numeric columns at indices 0 (nlcd_class) and 1 (manning_n). Optional
    columns (e.g. ``description``) are tolerated.

    Args:
        csv_path: optional explicit override (tests use this to inject a fixture
            CSV with only a subset of classes); ``None`` reads
            ``MANNING_MAPPING_PATH``.

    Returns:
        ``{nlcd_class_int: manning_n_float}`` -- every row in the CSV becomes
        an entry; duplicates are last-wins with a logged warning.

    Raises:
        ManningMappingError("MANNING_MAPPING_LOAD_FAILED", …): the CSV is missing,
            empty, or unparseable.
    """
    path = Path(csv_path) if csv_path is not None else MANNING_MAPPING_PATH
    if not path.exists():
        raise ManningMappingError(
            "MANNING_MAPPING_LOAD_FAILED",
            message=f"Manning's mapping CSV not found at {path}",
            details={"path": str(path)},
        )

    mapping: dict[int, float] = {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            # Skip leading comments + blank lines.
            data_lines = [
                line
                for line in fh.readlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
        if not data_lines:
            raise ManningMappingError(
                "MANNING_MAPPING_LOAD_FAILED",
                message=f"Manning's mapping CSV at {path} is empty after stripping comments",
                details={"path": str(path)},
            )
        reader = csv.reader(data_lines)
        header = next(reader, None)
        if header is None or not header:
            raise ManningMappingError(
                "MANNING_MAPPING_LOAD_FAILED",
                message=f"Manning's mapping CSV at {path} has no header row",
                details={"path": str(path)},
            )
        for row_idx, row in enumerate(reader, start=2):
            if not row or all(not c.strip() for c in row):
                continue
            if len(row) < 2:
                continue
            try:
                cls = int(row[0].strip())
                n_val = float(row[1].strip())
            except (ValueError, IndexError):
                logger.warning(
                    "manning_mapping row %d not parseable: %r (skipped)",
                    row_idx,
                    row,
                )
                continue
            if cls in mapping:
                logger.warning(
                    "manning_mapping duplicate nlcd_class=%d at row %d "
                    "(was %.4f, now %.4f) — last-wins",
                    cls,
                    row_idx,
                    mapping[cls],
                    n_val,
                )
            mapping[cls] = n_val
    except ManningMappingError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManningMappingError(
            "MANNING_MAPPING_LOAD_FAILED",
            message=f"Manning's mapping CSV at {path} could not be parsed: {exc}",
            details={"path": str(path)},
        ) from exc

    if not mapping:
        raise ManningMappingError(
            "MANNING_MAPPING_LOAD_FAILED",
            message=f"Manning's mapping CSV at {path} parsed to an empty mapping",
            details={"path": str(path)},
        )
    return mapping
