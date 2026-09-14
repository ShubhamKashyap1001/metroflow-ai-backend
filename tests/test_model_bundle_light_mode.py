"""Tests for the Render Free (512MB) production-model memory fix:
AI_MODEL_LIGHT_MODE now defaults to True, and app.ai_engine.model_bundle
prunes a loaded bundle down to just its winning model before it's ever
cached - see app/core/config.py and app/ai_engine/model_bundle.py.
"""
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from app.ai_engine import model_bundle
from app.ai_engine.prediction import crowd_predictor
from app.core.config import settings


class _FakeModel:
    """Minimal stand-in for a fitted sklearn/xgboost regressor - just
    enough surface (.predict()) for prune_to_winner()/the predictor
    code to treat it like a real model."""

    def __init__(self, value: float):
        self.value = value

    def predict(self, X):
        return np.array([self.value] * len(X))


def _two_model_bundle(winner_name: str = "xgboost") -> dict:
    """A bundle shaped exactly like a real saved crowd/delay/frequency
    .pkl before pruning: two fully-trained candidates under `models`."""
    rf = _FakeModel(100.0)
    xgb = _FakeModel(200.0)
    candidates = {"random_forest": rf, "xgboost": xgb}
    return {
        "model": candidates[winner_name],
        "model_name": winner_name,
        "features": ["station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour"],
        "models": candidates,
    }


class TestLightModeDefaultsOn:
    """1. AI_MODEL_LIGHT_MODE is enabled by default."""

    def test_light_mode_enabled_by_default(self):
        assert settings.AI_MODEL_LIGHT_MODE is True


class TestPruneToWinner:
    """2. A bundle containing RandomForest + XGBoost is reduced to only
    its winning model."""

    def test_prunes_to_single_winning_model(self, monkeypatch):
        monkeypatch.setattr(settings, "AI_MODEL_LIGHT_MODE", True)
        bundle = _two_model_bundle(winner_name="xgboost")

        pruned = model_bundle.prune_to_winner(bundle)

        assert set(pruned["models"].keys()) == {"xgboost"}
        assert pruned["model_name"] == "xgboost"
        assert pruned["model"] is pruned["models"]["xgboost"]

    def test_leaves_bundle_untouched_when_light_mode_off(self, monkeypatch):
        monkeypatch.setattr(settings, "AI_MODEL_LIGHT_MODE", False)
        bundle = _two_model_bundle(winner_name="random_forest")

        pruned = model_bundle.prune_to_winner(bundle)

        assert set(pruned["models"].keys()) == {"random_forest", "xgboost"}

    def test_none_bundle_passes_through(self, monkeypatch):
        monkeypatch.setattr(settings, "AI_MODEL_LIGHT_MODE", True)
        assert model_bundle.prune_to_winner(None) is None


class TestPredictorInterfaceUnchanged:
    """3. The predictor still returns the same prediction interface
    after a bundle has been pruned to one model."""

    def test_predict_crowd_shape_unchanged_after_pruning(self, monkeypatch):
        pruned_bundle = model_bundle.prune_to_winner(_two_model_bundle(winner_name="xgboost"))
        monkeypatch.setattr(crowd_predictor, "_load_model", lambda: pruned_bundle)

        result = crowd_predictor.predict_crowd(station_id=1)

        # Same top-level interface as an unpruned bundle would return -
        # only the *number* of entries in `models` differs (1 instead
        # of 2), not the shape of the response.
        assert set(result.keys()) == {
            "station_id", "target_datetime", "predicted_count",
            "confidence", "model_version", "models",
        }
        assert result["predicted_count"] == 200
        assert result["model_version"] == "xgboost_v1"
        assert set(result["models"].keys()) == {"xgboost"}


class TestJoblibLoadOncePerKey:
    """4. joblib.load happens at most once per model key."""

    def test_get_or_load_calls_joblib_load_once_for_repeated_calls(self, monkeypatch):
        monkeypatch.setattr(model_bundle, "_registry", {})
        monkeypatch.setattr(model_bundle, "_load_counts", {k: 0 for k in model_bundle._VALID_KEYS})
        monkeypatch.setattr(model_bundle.os.path, "exists", lambda path: True)

        fake_load = MagicMock(return_value=_two_model_bundle())
        monkeypatch.setattr(model_bundle.joblib, "load", fake_load)

        first = model_bundle.get_or_load("crowd", "fake/crowd_model.pkl")
        second = model_bundle.get_or_load("crowd", "fake/crowd_model.pkl")
        third = model_bundle.get_or_load("crowd", "fake/crowd_model.pkl")

        assert fake_load.call_count == 1
        assert model_bundle.get_load_counts()["crowd"] == 1
        assert first is second is third
