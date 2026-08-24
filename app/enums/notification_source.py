import enum

class NotificationSource(str, enum.Enum):
    EMAIL = "email"
    OPERATOR = "operator"
    SYSTEM = "system"
    SYSTEM_FAILURE = "system_failure"
