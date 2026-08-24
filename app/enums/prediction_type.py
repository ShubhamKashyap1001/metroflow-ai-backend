import enum

class PredictionType(str, enum.Enum):
    CROWD = "crowd"
    DEMAND = "demand"
    DELAY = "delay"
    FREQUENCY = "frequency"
