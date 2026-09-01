import enum

class NotificationDispatchKind(str, enum.Enum):
    """Which alert_service dispatch function a NotificationDispatchJob
    row resumes into - see app/services/notification_dispatch_queue.py."""

    ALERT_CREATED = "alert_created"
    ALERT_RESOLVED = "alert_resolved"
