"""Regression test for the 2026-07-30 Kosutarica incident.

WHAT HAPPENED
-------------
`agents/satellite/requirements.txt` declares `scipy==1.16.2`, but the harness
venv (`.venv-e2e`) did not have it installed. `sar_change_detection` imports
scipy lazily, INSIDE its functions (`from scipy.ndimage import uniform_filter`
et al), so the missing package surfaced as an ImportError at call time rather
than at module import. `processor.py`'s bare `except Exception` around
`detect_flood_change` caught it, logged one warning line, and continued to the
uncalibrated absolute-threshold path.

The run then completed "successfully":

    index_units        dB_uncalibrated
    index_calibrated   False
    affected_area_km2  0.0
    total_zones        0
    pipeline_status    complete_zero_zones

That is indistinguishable, to every downstream consumer, from "the detector
ran and found no flood" — on an event whose reference maps 1.617 km2 of
confirmed flood. It cost ~44 minutes and 2,636 MB, and the number it produced
would have entered the validation set as a scored zero.

WHY A TEST AND NOT JUST `pip install scipy`
-------------------------------------------
Installing scipy fixes this instance. It does not stop the next one — any
environment that ships without a lazily-imported dependency reproduces it
exactly, and the audit fields (`index_units`/`index_calibrated`), which are
this pipeline's designed defence against "which method actually ran", recorded
the fallback FAITHFULLY and still did not prevent the bad number. They are
diagnostic, not preventive: someone has to read them.

So the fix under test is behavioural — a missing dependency must RAISE, not
degrade. A data condition (misaligned baseline, too few reference scenes) is a
legitimate reason to fall back; a missing import means the deployment cannot
perform the analysis it advertises on ANY input, and continuing produces a
physically meaningless number wearing a valid result's clothes.

WHAT THIS TEST ASSERTS
----------------------
1. An ImportError from the change-detection import path propagates as a
   RuntimeError naming the dependency — it does NOT return a result dict.
2. The error message says "not a property of the imagery", so the failure
   cannot be misread as a data finding.
3. Non-ImportError failures STILL degrade to the fallback (the pre-existing,
   correct behaviour is not broken by the fix).
4. scipy is actually importable in the environment running the suite, with the
   exact symbols `sar_change_detection` uses — the environment assertion that
   would have caught the original incident before any download was spent.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[1]
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))


def test_scipy_symbols_used_by_change_detection_are_importable():
    """The environment assertion that would have caught the incident up front.

    `sar_change_detection` imports these four symbols lazily inside functions,
    so a missing scipy is invisible until a real multi-GB run reaches change
    detection. Asserting them here costs nothing and fails in milliseconds.
    """
    from scipy.ndimage import (  # noqa: F401
        binary_opening,
        label,
        minimum_filter,
        uniform_filter,
    )


def _scipyless_sar_module():
    """A stand-in for `sar_change_detection` whose `detect_flood_change`
    raises ImportError at CALL time — reproducing a scipy-less deployment
    exactly, since the real module's scipy imports are inside its functions."""

    class _NoScipyModule:
        @staticmethod
        def detect_flood_change(*_a, **_kw):
            raise ImportError("No module named 'scipy'")

    return _NoScipyModule()


def test_real_processor_raises_on_missing_dependency(monkeypatch):
    """Exercise the REAL production branch in processor.py.

    This calls `processor.process_satellite_imagery`'s change-detection block
    by driving `_attempt_clip`'s index computation with a scipy-less
    `sar_change_detection` in `sys.modules`, so the lazy
    `from sar_change_detection import detect_flood_change` inside processor
    resolves to the failing stub. Nothing about the expected behaviour is
    reimplemented in the test — the RuntimeError must come out of
    processor.py's own code, or this fails.
    """
    import numpy as np

    import processor

    monkeypatch.setitem(sys.modules, "sar_change_detection", _scipyless_sar_module())

    # Minimal S1 state that reaches the `if pre_event_vv:` branch.
    vv = np.full((8, 8), 100.0, dtype="float32")
    clipped = {
        "bands": {"VV": vv},
        "tci": None,
        "transform": None,
        "crs": None,
        "shape": vv.shape,
        "mask": np.ones(vv.shape, dtype=bool),
    }

    with pytest.raises(RuntimeError) as excinfo:
        processor.calculate_indices(
            clipped,
            "sentinel-1",
            "flood",
            pre_event_vv=[np.full((8, 8), 100.0, dtype="float32")],
            orbit_direction="DESCENDING",
        )

    msg = str(excinfo.value)
    assert "missing dependency" in msg
    # This sentence is the whole point of the fix: the failure must not be
    # readable as a statement about the imagery.
    assert "not a property of the imagery" in msg
    assert "uncalibrated" in msg


def test_data_failures_still_degrade_to_fallback(monkeypatch):
    """The fix must NOT turn ordinary data failures into hard errors.

    A misaligned baseline or too-few-reference-scenes is a legitimate reason
    to fall back and say so. Only a missing dependency raises.
    """
    import numpy as np

    import processor

    class _DataFailureModule:
        @staticmethod
        def detect_flood_change(*_a, **_kw):
            raise ValueError("all input arrays must have the same shape")

    monkeypatch.setitem(sys.modules, "sar_change_detection", _DataFailureModule())

    vv = np.full((8, 8), 100.0, dtype="float32")
    clipped = {
        "bands": {"VV": vv},
        "tci": None,
        "transform": None,
        "crs": None,
        "shape": vv.shape,
        "mask": np.ones(vv.shape, dtype=bool),
    }

    # Must NOT raise — degrades to the absolute path, as before the fix.
    out = processor.calculate_indices(
        clipped,
        "sentinel-1",
        "flood",
        pre_event_vv=[np.full((8, 8), 100.0, dtype="float32")],
        orbit_direction="DESCENDING",
    )
    assert out is not None
    # And it must be HONEST about having used the uncalibrated path.
    assert out.get("index_calibrated") is False


def test_processor_source_distinguishes_import_error_from_data_failure():
    """Guard the SHAPE of the fix: an `except ImportError` branch that raises
    must sit BEFORE the general `except Exception` branch that degrades.

    Ordering matters and is easy to undo in a refactor — ImportError is a
    subclass of Exception, so a general handler placed first would swallow it
    again and silently restore the original bug.
    """
    src = (AGENT_DIR / "processor.py").read_text(encoding="utf-8")
    idx_import = src.find("except ImportError as exc:")
    idx_general = src.find("SAR change detection failed (%s)")
    assert idx_import != -1, "the except-ImportError branch is gone"
    assert idx_general != -1, "the degrade-on-data-failure branch is gone"
    assert idx_import < idx_general, (
        "except ImportError must precede the general except Exception, or the "
        "general handler swallows it and the 2026-07-30 bug is back"
    )
