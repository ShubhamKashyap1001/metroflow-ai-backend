import enum

class NotificationStatus(str, enum.Enum):
    SENT = "sent"
    FAILED = "failed"
