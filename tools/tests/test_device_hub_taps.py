from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

MAC_ACCEPTANCE = Path(__file__).resolve().parents[1] / "acceptance" / "mac"
sys.path.insert(0, str(MAC_ACCEPTANCE))
spec = importlib.util.spec_from_file_location(
    "device_hub_taps", MAC_ACCEPTANCE / "device_hub_taps.py"
)
device_hub_taps = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = device_hub_taps
spec.loader.exec_module(device_hub_taps)


def test_compare_uses_monotonic_spacing_not_wall_clock_spacing():
    result = device_hub_taps.compare(
        cadence=250,
        taps=3,
        offsets=[0, 250, 500],
        received={"epoch_ms": [1000, 2250, 2320], "uptime_ms": [5000, 5250, 5500]},
    )

    assert result["every_tap_arrived"] is True
    assert result["cadence_held"] is True
    assert result["cadence_evaluable"] is True
    assert result["app_spacing_ms"] == [250, 250]
    assert result["worst_cadence_drift_ms"] == 0


def test_compare_rejects_evidence_without_monotonic_timestamps():
    with pytest.raises(ValueError, match="uptime_ms"):
        device_hub_taps.compare(
            cadence=250, taps=2, offsets=[0, 250], received={"epoch_ms": [1000, 1250]}
        )


def test_compare_requires_equal_timestamp_stream_lengths():
    with pytest.raises(ValueError, match="equal lengths"):
        device_hub_taps.compare(
            cadence=250,
            taps=2,
            offsets=[0, 250],
            received={"epoch_ms": [1000], "uptime_ms": [5000, 5250]},
        )


def test_compare_accepts_single_tap_without_claiming_cadence_evidence():
    result = device_hub_taps.compare(
        cadence=250,
        taps=1,
        offsets=[0],
        received={"epoch_ms": [1000], "uptime_ms": [5000]},
    )

    assert result["every_tap_arrived"] is True
    assert result["cadence_evaluable"] is False
    assert result["cadence_held"] is True


def test_compare_reports_missing_tap_and_bad_cadence_separately():
    missing = device_hub_taps.compare(
        cadence=250,
        taps=3,
        offsets=[0, 250, 500],
        received={"epoch_ms": [1000, 1250], "uptime_ms": [5000, 5500]},
    )
    late = device_hub_taps.compare(
        cadence=250,
        taps=3,
        offsets=[0, 250, 500],
        received={"epoch_ms": [1000, 1250, 1500], "uptime_ms": [5000, 5500, 6000]},
    )

    assert missing["every_tap_arrived"] is False
    assert missing["cadence_held"] is False
    assert late["every_tap_arrived"] is True
    assert late["cadence_held"] is False
