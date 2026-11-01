import enum

class NotificationDispatchKind(str, enum.Enum):

    ALERT_CREATED = "alert_created"
    ALERT_RESOLVED = "alert_resolved"
