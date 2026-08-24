import enum

class ScheduleStatus(str, enum.Enum):
    ON_TIME = "on_time"
    DELAYED = "delayed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
