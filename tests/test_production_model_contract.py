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
    monkeypatch.setattr(model_bundle, "_registry", {})
    monkeypatch.setattr(model_bundle, "_load_counts", {k: 0 for k in model_bundle._VALID_KEYS})

class TestRealArtifactsExist:
    def test_all_three_pkl_files_present(self):
        missing = [key for key, path in MODEL_PATHS.items() if not os.path.exists(path)]
        assert not missing, f"Missing production model artifact(s): {missing}"

class TestRealArtifactsLoad:

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

    @pytest.mark.parametrize("key", ["crowd", "delay", "frequency"])
    def test_repeated_get_or_load_hits_cache(self, key, monkeypatch):
        _fresh_registry(monkeypatch)
        first = model_bundle.get_or_load(key, MODEL_PATHS[key])
        second = model_bundle.get_or_load(key, MODEL_PATHS[key])
        assert first is second
        assert model_bundle.get_load_counts()[key] == 1

class TestFeatureOrderMatchesContract:

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

    def test_ai_eager_warmup_is_false(self):
        assert settings.AI_EAGER_WARMUP is False

    def test_ai_model_light_mode_is_true(self):
        assert settings.AI_MODEL_LIGHT_MODE is True

    def test_web_concurrency_is_one(self):
        assert settings.WEB_CONCURRENCY == 1

class TestNewBundlesHaveNoUnusedCandidates:

    @pytest.mark.parametrize("key", ["crowd", "delay", "frequency"])
    def test_bundle_has_no_extra_candidates(self, key, monkeypatch):
        _fresh_registry(monkeypatch)
        bundle = model_bundle.get_or_load(key, MODEL_PATHS[key])
        assert bundle is not None
        candidates = bundle.get("models")
        assert not candidates or len(candidates) == 1

class TestInferenceIsSingleThreaded:

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
        model_bundle._pin_inference_threads(est)  
        assert not hasattr(est, "n_jobs")

class TestEndToEndPredictionsSucceed:

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
