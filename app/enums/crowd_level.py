import enum

class CrowdLevel(str, enum.Enum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"

    @staticmethod
    def from_ratio(ratio: float) -> "CrowdLevel":
        """Derive a crowd level from occupancy ratio (current_count / capacity)."""
        if ratio < 0.4:
            return CrowdLevel.LOW
        if ratio < 0.7:
            return CrowdLevel.MODERATE
        if ratio < 0.9:
            return CrowdLevel.HIGH
        return CrowdLevel.CRITICAL
