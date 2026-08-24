import enum

class TrainStatus(str, enum.Enum):
    ACTIVE = "active"
    IN_SERVICE = "in_service"
    DELAYED = "delayed"
    MAINTENANCE = "maintenance"
    OUT_OF_SERVICE = "out_of_service"
