import enum

class NotificationChannel(str, enum.Enum):
    EMAIL = "email"
    SMS = "sms"
