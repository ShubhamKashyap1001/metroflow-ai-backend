from datetime import datetime

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import model_validator

from app.enums.prediction_type import PredictionType

class PredictionCreate(BaseModel):
    station_id: int
    predicted_count: int | None = None
    confidence: float | None = None
    prediction_type: PredictionType = PredictionType.CROWD
    predicted_value: float = 0
    target_datetime: datetime | None = None
    model_version: str | None = None

class PredictionResponse(BaseModel):
    id: int
    station_id: int
    predicted_count: int | None = None
    confidence: float | None
    prediction_type: PredictionType
    predicted_value: float
    target_datetime: datetime | None = None
    model_version: str | None = None
    created_at: datetime
    data_source: str | None = None
    inputs_summary: str | None = None
    confidence_note: str | None = None

    models: dict[str, dict] = {}

    @model_validator(mode="after")
    def explain_prediction(self):
        is_fallback = self.model_version == "heuristic_fallback"
        if self.prediction_type in (PredictionType.CROWD, PredictionType.DEMAND):
            self.data_source = "Calendar heuristic fallback" if is_fallback else "Trained passenger-flow model"
            self.inputs_summary = (
                "Local hour, weekday/weekend and peak-hour pattern; live passenger count is not used."
                if is_fallback else
                "Station identifier and local calendar features; current live passenger count is not an input."
            )
            self.confidence_note = (
                "No calibrated model confidence is available for this heuristic."
                if is_fallback else
                "Agreement among model trees; an internal consistency score, not a probability of being correct."
                if self.confidence is not None else
                "This model does not provide a confidence score for this prediction."
            )
            if is_fallback:
                self.confidence = None
        elif self.prediction_type == PredictionType.DELAY:
            self.data_source = "Heuristic delay estimate" if is_fallback else "Trained delay model"
            self.inputs_summary = (
                "Predicted passenger demand and a simple delay formula."
                if is_fallback else
                "Train/station context, predicted passenger demand and local calendar features; weather is a default proxy, not a live feed."
            )
            self.confidence = None
            self.confidence_note = "A calibrated confidence score is not calculated for delay predictions."
        else:
            self.data_source = "Peak/off-peak heuristic" if is_fallback else "Trained train-frequency model"
            self.inputs_summary = "Station identifier and local calendar/peak-hour features; current live passenger count is not an input."
            self.confidence = None
            self.confidence_note = "A calibrated confidence score is not calculated for frequency recommendations."
        return self

    model_config = ConfigDict(
        from_attributes=True
    )

class CrowdPredictionRequest(BaseModel):
    station_id: int
    target_datetime: datetime | None = None

class DemandForecastRequest(BaseModel):
    station_id: int
    hours_ahead: int = 6

class DelayPredictionRequest(BaseModel):
    train_id: int
    station_id: int

class FrequencyRecommendationRequest(BaseModel):
    station_id: int
    is_peak_hour: bool = False

class SmartRecommendation(BaseModel):
    station_id: int
    title: str
    detail: str
    severity: str = "info"

class AggregateHourPoint(BaseModel):
    hour: int
    predicted_count: int
    is_peak_hour: bool

class AggregateTrafficPattern(BaseModel):
    station_count: int
    forecast_source: str = "trained_model"
    hourly_forecast: list[AggregateHourPoint]
    peak_hour: int
    peak_predicted_count: int
    quietest_hour: int

class FeatureImportanceItem(BaseModel):
    feature: str
    importance: float

class CrowdSingleModelMetrics(BaseModel):
    available: bool
    model_name: str | None = None
    mae: float | None = None
    mape_pct: float | None = None
    r2: float | None = None
    accuracy: float | None = None
    macro_f1: float | None = None
    critical_recall: float | None = None
    trained_rows: int | None = None
    test_rows: int | None = None
    classes: list[str] = []
    confusion_matrix: list[list[int]] = []
    feature_importance: list[FeatureImportanceItem] = []

class CrowdModelMetrics(BaseModel):
    available: bool
    model_name: str | None = None
    mae: float | None = None
    mape_pct: float | None = None
    r2: float | None = None
    accuracy: float | None = None
    macro_f1: float | None = None
    critical_recall: float | None = None
    trained_rows: int | None = None
    test_rows: int | None = None
    classes: list[str] = []
    confusion_matrix: list[list[int]] = []
    feature_importance: list[FeatureImportanceItem] = []

    models: dict[str, CrowdSingleModelMetrics] = {}

class RegressionSingleModelMetrics(BaseModel):
    available: bool
    model_name: str | None = None
    mae: float | None = None
    mape_pct: float | None = None
    r2: float | None = None
    trained_rows: int | None = None
    test_rows: int | None = None
    feature_importance: list[FeatureImportanceItem] = []

class RegressionModelMetrics(BaseModel):
    available: bool
    model_name: str | None = None
    mae: float | None = None
    mape_pct: float | None = None
    r2: float | None = None
    trained_rows: int | None = None
    test_rows: int | None = None
    feature_importance: list[FeatureImportanceItem] = []
    models: dict[str, RegressionSingleModelMetrics] = {}
