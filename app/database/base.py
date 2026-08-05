# from app.database.database import Base

# from app.models.user_profile import UserProfile
# from app.models.station import Station
# from app.models.train import Train
# from app.models.route import Route
# from app.models.train_route import TrainRoute
# from app.models.checkin import CheckIn
# from app.models.checkout import CheckOut
# from app.models.crowd_log import CrowdLog
# from app.models.prediction import Prediction
# from app.models.alert import Alert


from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass