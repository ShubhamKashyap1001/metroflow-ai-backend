"""Tests for the production model integration fix (Sept 2026 retrain).

The new crowd_model.pkl / delay_model.pkl / frequency_model.pkl
artifacts each ship ONLY their single winning model (no `models` dict
of multiple candidates, unlike older bundles) - see
app/ai_engine/model_bundle.py's module docstring. These tests confirm:

1. Each of the 3 real .pkl files under app/ai_engine/saved_models/
   loads successfully via the shared model_bundle loader.
2. Each loads at most once per process (shared registry).
3. Each bundle's `features` list is exactly the order each predictor
   expects - see KNOWN_FEATURES / the hardcoded column lists in
   crowd_predictor.py, delay_predictor.py, frequency_predictor.py.
4. model_bundle.validate_feature_contract() correctly accepts the real
   feature lists and rejects an unrecognized feature name.
5. Loading a model bundle never reads the large source CSV datasets
   (passenger_flow.csv.gz / train_operations.csv.gz) - those are only
   ever read by the separate *_metrics.py dashboard-evaluation path,
   never by the prediction hot path.
6. AI_EAGER_WARMUP / AI_MODEL_LIGHT_MODE / WEB_CONCURRENCY keep their
   Render-Free-safe defaults.

Requires the pinned runtime dependencies (scikit-learn==1.8.0,
xgboost==2.1.3, joblib==1.5.3 - see requirements.txt) to actually
unpickle the real artifacts; run this inside the project's normal
dev/CI environment (or the Docker build), not a bare interpreter
missing those packages.
"""
import os

import pandas as pd
import pytest

from app.ai_engine import model_bundle
from app.ai_engine.prediction import crowd_predictor, delay_predictor, frequency_predictor
from app.core.config import settings

SAVED_MODELS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "app", "ai_engine", "saved_models"
)

MODEL_PATHS = {
    "crowd": os.path.join(SAVED_MODELS_DIR, "crowd_model.pkl"),
    "delay": os.path.join(SAVED_MODELS_DIR, "delay_model.pkl"),
    "frequency": os.path.join(SAVED_MODELS_DIR, "frequency_model.pkl"),
}

EXPECTED_FEATURES = {
    "crowd": ["station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour"],
    "frequency": ["station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour"],
    "delay": [
        "station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour",
        "passenger_count", "capacity_passengers", "train_age_days", "weather_code",
    ],
}


def _fresh_registry(monkeypatch):
    """Isolate model_bundle's process-wide registry/counters for a test,
    so earlier tests in the same session don't leave a cached bundle
    behind."""
    monkeypatch.setattr(model_bundle, "_registry", {})
    monkeypatch.setattr(model_bundle, "_load_counts", {k: 0 for k in model_bundle._VALID_KEYS})


class TestRealArtifactsExist:
    def test_all_three_pkl_files_present(self):
        missing = [key for key, path in MODEL_PATHS.items() if not os.path.exists(path)]
        assert not missing, f"Missing production model artifact(s): {missing}"


class TestRealArtifactsLoad:
    """1. New crowd/delay/frequency models load successfully."""

    @pytest.mark.parametrize("key", ["crowd", "delay", "frequency"])
    def test_bundle_loads_and_has_required_keys(self, key, monkeypatch):
        _fresh_registry(monkeypatch)
        bundle = model_bundle.get_or_load(key, MODEL_PATHS[key])
        assert bundle is not None, f"{key}_model.pkl failed to load - see logged warning"
        assert "model" in bundle
        assert "model_name" in bundle
        assert "features" in bundle
        assert hasattr(bundle["model"], "predict")


class TestLoadedOncePerKey:
    """4. Each model is loaded at most once per process."""

    @pytest.mark.parametrize("key", ["crowd", "delay", "frequency"])
    def test_repeated_get_or_load_hits_cache(self, key, monkeypatch):
        _fresh_registry(monkeypatch)
        first = model_bundle.get_or_load(key, MODEL_PATHS[key])
        second = model_bundle.get_or_load(key, MODEL_PATHS[key])
        assert first is second
        assert model_bundle.get_load_counts()[key] == 1


class TestFeatureOrderMatchesContract:
    """5. Feature order exactly matches the new model contract."""

    @pytest.mark.parametrize("key", ["crowd", "delay", "frequency"])
    def test_saved_feature_order(self, key, monkeypatch):
        _fresh_registry(monkeypatch)
        bundle = model_bundle.get_or_load(key, MODEL_PATHS[key])
        assert bundle is not None
        assert bundle["features"] == EXPECTED_FEATURES[key]


class TestFeatureContractValidation:
    def test_accepts_real_crowd_features(self):
        model_bundle.validate_feature_contract(
            EXPECTED_FEATURES["crowd"], crowd_predictor.KNOWN_FEATURES, context="test"
        )

    def test_accepts_real_delay_features(self):
        model_bundle.validate_feature_contract(
            EXPECTED_FEATURES["delay"], delay_predictor.KNOWN_FEATURES, context="test"
        )

    def test_accepts_real_frequency_features(self):
        model_bundle.validate_feature_contract(
            EXPECTED_FEATURES["frequency"], frequency_predictor.KNOWN_FEATURES, context="test"
        )

    def test_rejects_unknown_feature_name(self):
        with pytest.raises(model_bundle.ModelFeatureContractError):
            model_bundle.validate_feature_contract(
                ["station_id", "totally_unexpected_feature"],
                crowd_predictor.KNOWN_FEATURES,
                context="test",
            )


class TestModelLoadingSkipsLargeDatasets:
    """14. Model initialization must not load passenger_flow.csv.gz /
    train_operations.csv.gz / any other source dataset - only the
    small .pkl bundle itself."""

    @pytest.mark.parametrize("key", ["crowd", "delay", "frequency"])
    def test_get_or_load_never_calls_read_csv(self, key, monkeypatch):
        _fresh_registry(monkeypatch)

        def _forbidden_read_csv(*args, **kwargs):
            raise AssertionError(
                "model_bundle.get_or_load() must not read any CSV dataset "
                "while loading a model bundle"
            )

        monkeypatch.setattr(pd, "read_csv", _forbidden_read_csv)
        bundle = model_bundle.get_or_load(key, MODEL_PATHS[key])
        assert bundle is not None


class TestRenderFreeMemoryDefaults:
    """11/14/15/16 (final report items) - Render Free safe defaults
    stay untouched by this integration."""

    def test_ai_eager_warmup_is_false(self):
        assert settings.AI_EAGER_WARMUP is False

    def test_ai_model_light_mode_is_true(self):
        assert settings.AI_MODEL_LIGHT_MODE is True

    def test_web_concurrency_is_one(self):
        assert settings.WEB_CONCURRENCY == 1


class TestNewBundlesHaveNoUnusedCandidates:
    """New production bundles ship only their winning model - there is
    no second candidate sitting in memory to prune."""

    @pytest.mark.parametrize("key", ["crowd", "delay", "frequency"])
    def test_bundle_has_no_extra_candidates(self, key, monkeypatch):
        _fresh_registry(monkeypatch)
        bundle = model_bundle.get_or_load(key, MODEL_PATHS[key])
        assert bundle is not None
        candidates = bundle.get("models")
        assert not candidates or len(candidates) == 1


class TestInferenceIsSingleThreaded:
    """CPU/RAM safety: both RandomForestRegressor and XGBRegressor are
    saved with n_jobs=-1 (trained-time default) - model_bundle must
    pin this to n_jobs=1 right after loading, so a single-row
    prediction never spins up a host-wide thread pool on this
    WEB_CONCURRENCY=1 instance. This must hold for the REAL loaded
    model, not just a synthetic stand-in."""

    @pytest.mark.parametrize("key", ["crowd", "delay", "frequency"])
    def test_loaded_model_n_jobs_pinned_to_one(self, key, monkeypatch):
        _fresh_registry(monkeypatch)
        bundle = model_bundle.get_or_load(key, MODEL_PATHS[key])
        assert bundle is not None
        model = bundle["model"]
        assert not hasattr(model, "n_jobs") or model.n_jobs == 1

    def test_pin_inference_threads_sets_n_jobs_one(self):
        class _FakeEstimator:
            n_jobs = -1

        est = _FakeEstimator()
        model_bundle._pin_inference_threads(est)
        assert est.n_jobs == 1

    def test_pin_inference_threads_skips_estimator_without_n_jobs(self):
        class _NoNJobs:
            pass

        est = _NoNJobs()
        model_bundle._pin_inference_threads(est)  # must not raise
        assert not hasattr(est, "n_jobs")


class TestEndToEndPredictionsSucceed:
    """6/7/8. A normal crowd/delay/frequency prediction succeeds against
    the real artifacts and keeps the existing response shape."""

    def test_predict_crowd(self, monkeypatch):
        _fresh_registry(monkeypatch)
        result = crowd_predictor.predict_crowd(station_id=1)
        assert set(result.keys()) == {
            "station_id", "target_datetime", "predicted_count",
            "confidence", "model_version", "models",
        }
        assert result["model_version"] != "heuristic_fallback"

    def test_predict_delay(self, monkeypatch):
        _fresh_registry(monkeypatch)
        result = delay_predictor.predict_delay(station_id=1)
        assert set(result.keys()) == {
            "station_id", "target_datetime", "predicted_delay_minutes",
            "based_on_predicted_crowd", "model_version", "models",
        }
        assert result["model_version"] != "heuristic_fallback"

    def test_recommend_frequency(self, monkeypatch):
        _fresh_registry(monkeypatch)
        result = frequency_predictor.recommend_frequency(station_id=1)
        assert set(result.keys()) == {
            "station_id", "target_datetime", "is_peak_hour",
            "recommended_frequency_minutes", "model_version", "models",
        }
        assert result["model_version"] != "heuristic_fallback"
