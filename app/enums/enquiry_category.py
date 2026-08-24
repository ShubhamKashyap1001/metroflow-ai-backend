import enum

class EnquiryCategory(str, enum.Enum):
    GENERAL = "general"
    TICKETING = "ticketing"
    LOST_AND_FOUND = "lost_and_found"
    SAFETY = "safety"
    TECHNICAL = "technical"
    COMPLAINT = "complaint"
    SUGGESTION = "suggestion"
    OTHER = "other"
