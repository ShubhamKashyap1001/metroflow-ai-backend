import pandas as pd
import pytest
from pydantic import ValidationError

from app.database.seed_real_data import _derive_station_capacities
from app.enums.crowd_level import CrowdLevel
from app.schemas.crowd_log import CrowdLogCreate
from app.simulator import csv_replay_simulator as sim

# Real rows: station STN-AMD-RL-01, 2026-01-01, every hour the station
# was open that day - copied byte-for-byte from
# datasets/passenger_flow.csv.gz (see docs/crowd-data-correctness.md for the
# extraction command).
STATION_CODE = "STN-AMD-RL-01"
REAL_DAY_ROWS = [
    ("2026-01-01 05:00:00", 96, 106, 0.080, "Low"),
    ("2026-01-01 06:00:00", 101, 95, 0.084, "Low"),
    ("2026-01-01 07:00:00", 82, 82, 0.068, "Low"),
    ("2026-01-01 08:00:00", 89, 86, 0.074, "Low"),
    ("2026-01-01 09:00:00", 183, 192, 0.152, "Normal"),
    ("2026-01-01 10:00:00", 347, 357, 0.289, "Normal"),
    ("2026-01-01 11:00:00", 606, 631, 0.505, "Crowded"),
    ("2026-01-01 12:00:00", 415, 459, 0.346, "Normal"),
    ("2026-01-01 13:00:00", 207, 224, 0.172, "Normal"),
    ("2026-01-01 14:00:00", 99, 97, 0.083, "Low"),
    ("2026-01-01 15:00:00", 86, 81, 0.072, "Low"),
    ("2026-01-01 16:00:00", 70, 62, 0.058, "Low"),
    ("2026-01-01 17:00:00", 93, 90, 0.077, "Low"),
    ("2026-01-01 18:00:00", 173, 169, 0.144, "Low"),
    ("2026-01-01 19:00:00", 411, 446, 0.343, "Normal"),
    ("2026-01-01 20:00:00", 517, 502, 0.431, "Crowded"),
    ("2026-01-01 21:00:00", 413, 418, 0.344, "Normal"),
    ("2026-01-01 22:00:00", 173, 161, 0.144, "Low"),
]

# Hand-verified (see docs/crowd-data-correctness.md) running
# max(0, occupancy + entries - exits) over REAL_DAY_ROWS in order.
EXPECTED_OCCUPANCY_SEQUENCE = [0, 6, 6, 9, 0, 0, 0, 0, 0, 2, 7, 15, 18, 22, 0, 15, 10, 22]


def _rows_as_series(rows):
    for ts, entries, exits, crowding_index, label in rows:
        yield pd.Series({
            sim.COL_TIMESTAMP: pd.Timestamp(ts),
            sim.COL_ENTRIES: entries,
            sim.COL_EXITS: exits,
            "crowding_index": crowding_index,
            sim.COL_CROWD_LABEL: label,
        })


class TestOccupancyAccumulator:
    """Bug 1: current_count must be a real net-flow occupancy
    accumulator, not entries + exits (throughput)."""

    def setup_method(self):
        # Each test gets a clean accumulator - these are module-level
        # dicts in csv_replay_simulator.py, keyed by station_code.
        sim._occupancy_by_station.pop(STATION_CODE, None)
        sim._last_row_date_by_station.pop(STATION_CODE, None)

    def test_occupancy_never_equals_entries_plus_exits(self):
        """The old bug: current_count == entries + exits. Confirm the
        fix never produces that value for any of these real rows
        (entries+exits ranges from 168 to 1237 in this real day -
        occupancy never gets remotely close)."""
        for row in _rows_as_series(REAL_DAY_ROWS):
            count = sim._current_count_from_row(STATION_CODE, row)
            assert count != int(row[sim.COL_ENTRIES] + row[sim.COL_EXITS])

    def test_occupancy_matches_hand_verified_real_sequence(self):
        """The accumulator must reproduce the exact hand-verified
        sequence for this real day (see EXPECTED_OCCUPANCY_SEQUENCE
        and docs/crowd-data-correctness.md)."""
        actual = [sim._current_count_from_row(STATION_CODE, row) for row in _rows_as_series(REAL_DAY_ROWS)]
        assert actual == EXPECTED_OCCUPANCY_SEQUENCE

    def test_occupancy_never_negative(self):
        """Real rows in this dataset regularly have exits > entries in
        a single hour (e.g. 05:00 has 96 entries / 106 exits) - the
        accumulator must clamp at 0, never go negative."""
        for row in _rows_as_series(REAL_DAY_ROWS):
            assert sim._current_count_from_row(STATION_CODE, row) >= 0

    def test_occupancy_resets_at_real_day_boundary(self):
        """Feeding one real day, then a row from the next calendar
        date, must reset the accumulator to 0 before applying the new
        row's flow - grounded in the fact that (measured across the
        whole dataset) per-station daily net flow is ~1.5% of that
        day's total ridership, i.e. stations really do empty out
        overnight (see docs/crowd-data-correctness.md)."""
        for row in _rows_as_series(REAL_DAY_ROWS):
            sim._current_count_from_row(STATION_CODE, row)
        # Accumulator is non-zero at end of day 1 (see expected seq).
        assert sim._occupancy_by_station[STATION_CODE] == EXPECTED_OCCUPANCY_SEQUENCE[-1]

        next_day_row = pd.Series({
            sim.COL_TIMESTAMP: pd.Timestamp("2026-01-02 05:00:00"),
            sim.COL_ENTRIES: 50,
            sim.COL_EXITS: 10,
            sim.COL_CROWD_LABEL: "Low",
        })
        count = sim._current_count_from_row(STATION_CODE, next_day_row)
        # Reset to 0 THEN apply this row's flow (50 - 10), not
        # 22 (yesterday's leftover) + 50 - 10.
        assert count == 40


class TestCapacityDerivation:
    """Bug 2: capacity must be derived per-station from the dataset's
    own crowding_index column, not hardcoded to 5000 for everyone."""

    def test_derives_real_capacity_from_crowding_index(self):
        df = pd.DataFrame(
            [(STATION_CODE, e, x, ci) for _, e, x, ci, _ in REAL_DAY_ROWS],
            columns=["station_id", "entries", "exits", "crowding_index"],
        )
        capacities = _derive_station_capacities(df)
        # Median implied capacity for this station's real first-day
        # rows, hand-verified against the full dataset (see
        # docs/crowd-data-correctness.md) - lands close to the whole-dataset
        # median of ~2,438, not the old hardcoded 5000.
        assert 2000 <= capacities[STATION_CODE] <= 2800
        assert capacities[STATION_CODE] != 5000

    def test_ignores_zero_crowding_index_rows_to_avoid_div_by_zero(self):
        df = pd.DataFrame(
            [("S1", 100, 100, 0.0), ("S1", 200, 200, 0.2)],
            columns=["station_id", "entries", "exits", "crowding_index"],
        )
        capacities = _derive_station_capacities(df)
        # Only the second (non-zero-index) row should contribute:
        # (200+200)/0.2 = 2000.
        assert capacities["S1"] == 2000


class TestCrowdLevelThresholds:
    """Bug 5 (partial): CrowdLevel.from_ratio must match the real
    dataset's own Low/Normal/Crowded/Critically-Overcrowded cutoffs
    (0.15 / 0.35 / 0.6), not the old arbitrary 0.4/0.7/0.9."""

    @pytest.mark.parametrize("row", REAL_DAY_ROWS)
    def test_from_ratio_matches_real_label_for_every_row(self, row):
        _, entries, exits, crowding_index, label = row
        expected = {
            "Low": CrowdLevel.LOW,
            "Normal": CrowdLevel.MODERATE,
            "Crowded": CrowdLevel.HIGH,
            "Critically Overcrowded": CrowdLevel.CRITICAL,
        }[label]
        assert CrowdLevel.from_ratio(crowding_index) == expected

    def test_critically_overcrowded_real_row(self):
        # STN-AMD-RL-01, 2026-01-23 11:00 - a real row labeled
        # "Critically Overcrowded" (crowding_index 0.631, i.e. just
        # over the 0.6 cutoff).
        assert CrowdLevel.from_ratio(0.631) == CrowdLevel.CRITICAL


class TestNegativeOccupancyRejected:
    """Bug 4: CrowdLogCreate had no lower bound on current_count."""

    def test_negative_current_count_is_rejected(self):
        with pytest.raises(ValidationError):
            CrowdLogCreate(station_id=1, current_count=-5)

    def test_zero_current_count_is_accepted(self):
        payload = CrowdLogCreate(station_id=1, current_count=0)
        assert payload.current_count == 0

    def test_positive_real_occupancy_value_is_accepted(self):
        payload = CrowdLogCreate(station_id=1, current_count=EXPECTED_OCCUPANCY_SEQUENCE[-1])
        assert payload.current_count == 22
