import enum

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
        if ratio < LOW_MAX_RATIO:
            return CrowdLevel.LOW
        if ratio < MODERATE_MAX_RATIO:
            return CrowdLevel.MODERATE
        if ratio < HIGH_MAX_RATIO:
            return CrowdLevel.HIGH
        return CrowdLevel.CRITICAL
