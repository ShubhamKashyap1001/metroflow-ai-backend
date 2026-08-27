from datetime import datetime

from pydantic import BaseModel
from pydantic import ConfigDict

from app.enums.prediction_type import PredictionType

class PredictionCreate(BaseModel):
    station_id: int
    predicted_count: int | None = None
    confidence: float = 0
    prediction_type: PredictionType = PredictionType.CROWD
    predicted_value: float = 0
    target_datetime: datetime | None = None
    model_version: str | None = None

class PredictionResponse(BaseModel):
    id: int
    station_id: int
    predicted_count: int | None = None
    confidence: float
    prediction_type: PredictionType
    predicted_value: float
    target_datetime: datetime | None = None
    model_version: str | None = None
    # Per-candidate breakdown (currently "random_forest" and "xgboost"
    # keys), same pattern as CrowdModelMetrics.models - lets a caller
    # show both models side by side instead of only the winner that
    # the flat fields above mirror. Shape varies by prediction_type
    # (e.g. predicted_delay_minutes for delay,
    # recommended_frequency_minutes for frequency), so this is kept
    # loosely typed here rather than duplicating a schema per type.
    models: dict[str, dict] = {}
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
    # Per-candidate breakdown (currently "random_forest" and "xgboost"
    # keys) so a caller can show both models side by side instead of
    # only whichever one won on held-out MAE at training time. The
    # flat fields above still mirror the winner, for any older caller
    # that only reads those.
    models: dict[str, CrowdSingleModelMetrics] = {}

class RegressionSingleModelMetrics(BaseModel):
    """Same shape as CrowdSingleModelMetrics minus the
    classification-only fields (accuracy/macro_f1/critical_recall/
    classes/confusion_matrix) that don't apply to a pure regression
    target such as delay minutes or recommended headway minutes."""
    available: bool
    model_name: str | None = None
    mae: float | None = None
    mape_pct: float | None = None
    r2: float | None = None
    trained_rows: int | None = None
    test_rows: int | None = None
    feature_importance: list[FeatureImportanceItem] = []

class RegressionModelMetrics(BaseModel):
    """Live evaluation metrics for a pure-regression production model
    (delay or frequency), same role as CrowdModelMetrics plays for the
    crowd/demand model. The flat fields mirror the winning candidate;
    `models` carries every trained candidate (random_forest, xgboost)
    for side-by-side comparison."""
    available: bool
    model_name: str | None = None
    mae: float | None = None
    mape_pct: float | None = None
    r2: float | None = None
    trained_rows: int | None = None
    test_rows: int | None = None
    feature_importance: list[FeatureImportanceItem] = []
    models: dict[str, RegressionSingleModelMetrics] = {}