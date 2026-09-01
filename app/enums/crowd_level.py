import enum

# Phase 3 fix (docs/crowd-data-correctness.md, Bug 5): calibrated against the
# real dataset's own crowding_label boundaries (measured off
# passenger_flow.csv.gz's crowding_index column), not guessed.
LOW_MAX_RATIO = 0.15
MODERATE_MAX_RATIO = 0.35
HIGH_MAX_RATIO = 0.6

class CrowdLevel(str, enum.Enum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"

    @staticmethod
    def from_ratio(ratio: float) -> "CrowdLevel":
        """Derive a crowd level from a capacity-relative ratio, using the
        SAME thresholds the real dataset's crowding_label column uses
        (Low < 0.15 <= Normal < 0.35 <= Crowded < 0.6 <= Critically
        Overcrowded - measured directly off passenger_flow.csv.gz's
        crowding_index column). These used to be arbitrary (0.4 / 0.7 /
        0.9) and disagreed with the real per-row labels; see
        docs/crowd-data-correctness.md, Bug 5, for the measurement and the fix.
        Any caller that has to *fall back* to a ratio-based level (no
        real crowding_label available - e.g. a manual POST /crowd/
        reading, or a check-in/check-out delta) now agrees with the
        dataset's own boundaries instead of inventing new ones."""
        if ratio < LOW_MAX_RATIO:
            return CrowdLevel.LOW
        if ratio < MODERATE_MAX_RATIO:
            return CrowdLevel.MODERATE
        if ratio < HIGH_MAX_RATIO:
            return CrowdLevel.HIGH
        return CrowdLevel.CRITICAL
