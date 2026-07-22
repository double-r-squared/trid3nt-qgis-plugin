"""Regression guard for the pandas pin established in job-0056-infra-20260607.

OQ-54 (sfincs.py:1858): hydromt-sfincs 1.2.2 calls ``pd.Index.is_integer()``
which was removed in pandas 2.0 and 3.0. Pin holds it at the 2.2.x series
where it still works (deprecated).

OQ-55 (sfincs.py:2456): hydromt-sfincs 1.2.2 calls
``pd.date_range(..., freq="10T")`` where the ``"T"`` minute alias was removed
in pandas 3.0. Pin holds it at the 2.2.x series where it still works
(deprecated).

These tests guard against a future accidental pandas re-bump (e.g. ``>=3``)
that would re-introduce both upstream bugs. They exercise the exact code paths
that fail in pandas 3.x — not mocks, not unit-test substitutes; they call the
real pandas API.

Migration note: when hydromt-sfincs upstream ships a pandas-3-compatible
release (likely v1.2.3 or v2.0 RC), update the pyproject.toml pin AND update
or remove these guards accordingly.
"""

from __future__ import annotations

import warnings

import pandas as pd
import pytest


def test_oq54_range_index_is_integer_still_works() -> None:
    """OQ-54: pd.RangeIndex.is_integer() must not raise AttributeError.

    hydromt-sfincs 1.2.2 sfincs.py:1858 calls::

        gdf_locs.index.is_integer()

    This was removed in pandas >= 2.0 and completely absent in pandas 3.x.
    Under the pinned 2.2.x series it exists but emits a FutureWarning.
    The test asserts the call returns True for a RangeIndex (the expected
    production value) without raising.
    """
    idx = pd.RangeIndex(start=1, stop=5)
    # Suppress the FutureWarning emitted by pandas 2.2.x — the call still works.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        result = idx.is_integer()
    assert result is True, (
        f"pd.RangeIndex.is_integer() returned {result!r}; expected True. "
        "If this fails, the pandas pin in pyproject.toml has drifted — "
        "OQ-54 (sfincs.py:1858) will break at runtime."
    )


def test_oq55_date_range_freq_10T_still_works() -> None:
    """OQ-55: pd.date_range(freq='10T') must not raise ValueError.

    hydromt-sfincs 1.2.2 sfincs.py:2456 calls::

        pd.date_range(*self.get_model_time(), freq="10T")

    The ``"T"`` alias for minutes was deprecated in pandas 2.2 and removed
    in pandas 3.0. Under the pinned 2.2.x series it still resolves but emits
    a FutureWarning. The test asserts the call succeeds and produces the
    expected 10-element range.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        dr = pd.date_range("2026-01-01", periods=10, freq="10T")
    assert len(dr) == 10, (
        f"pd.date_range(freq='10T') produced {len(dr)} periods; expected 10. "
        "If this fails, the pandas pin in pyproject.toml has drifted — "
        "OQ-55 (sfincs.py:2456) will break at runtime."
    )
    # Confirm the interval is 10 minutes as expected by hydromt-sfincs internals.
    delta_minutes = (dr[1] - dr[0]).total_seconds() / 60
    assert delta_minutes == 10.0, (
        f"date_range interval is {delta_minutes} min; expected 10. "
        "Frequency alias '10T' resolved to an unexpected interval."
    )


def test_pandas_version_within_pin_bounds() -> None:
    """Assert the installed pandas version is within the job-0056 pin bounds.

    This is a belt-and-suspenders check: if the venv is ever re-resolved
    without the pin, the version will jump to 3.x and the OQ-54/OQ-55 guards
    above will already fail at the API level. This test surfaces the version
    violation explicitly with a clear error message.

    Pin: pandas >= 2.2, < 2.3 (see services/agent/pyproject.toml).
    """
    from packaging.version import Version

    v = Version(pd.__version__)
    assert Version("2.2") <= v < Version("2.3"), (
        f"Installed pandas {pd.__version__} is outside the job-0056 pin "
        "(>=2.2,<2.3). OQ-54 (is_integer) and OQ-55 (freq='10T') will both "
        "fail at runtime against hydromt-sfincs 1.2.2. Re-pin or upgrade "
        "hydromt-sfincs to a pandas-3-compatible release first."
    )
