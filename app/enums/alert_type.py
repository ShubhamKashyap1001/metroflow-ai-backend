import enum

class AlertType(str, enum.Enum):
    OVERCROWDING = "overcrowding"
    DELAY = "delay"
    EMERGENCY = "emergency"
    MAINTENANCE = "maintenance"
    INFO = "info"
