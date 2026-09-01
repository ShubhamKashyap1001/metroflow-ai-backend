import enum

class NotificationDispatchStatus(str, enum.Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
