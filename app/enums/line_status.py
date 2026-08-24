import enum

class LineStatus(str, enum.Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    MAINTENANCE = "maintenance"
