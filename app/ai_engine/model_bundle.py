import logging
import os
import threading

import joblib

from app.core.config import settings

logger = logging.getLogger(__name__)

class ModelFeatureContractError(RuntimeError):
    pass

def validate_feature_contract(bundle_features: list[str], known_features: set[str], *, context: str) -> None:
    unknown = [f for f in bundle_features if f not in known_features]
    if unknown:
        raise ModelFeatureContractError(
            f"{context}: model bundle expects unknown feature(s) {unknown!r} "
            f"(known: {sorted(known_features)!r})"
        )

_VALID_KEYS = ("crowd", "delay", "frequency")

_registry_lock = threading.Lock()
_registry: dict[str, dict | None] = {}

_load_counts: dict[str, int] = {key: 0 for key in _VALID_KEYS}

def _pin_inference_threads(model) -> None:
    if hasattr(model, "n_jobs"):
        try:
            model.n_jobs = 1
        except Exception:

            pass

    get_booster = getattr(model, "get_booster", None)
    if callable(get_booster):
        try:
            get_booster().set_param({"nthread": 1})
        except Exception:

            pass

def _pin_all_candidates_inference_threads(bundle: dict | None) -> None:
    if not bundle:
        return
    _pin_inference_threads(bundle.get("model"))
    for candidate in (bundle.get("models") or {}).values():
        _pin_inference_threads(candidate)

def get_or_load(key: str, path: str):
    if key not in _VALID_KEYS:
        raise ValueError(f"unknown model key {key!r} - expected one of {_VALID_KEYS}")

    if key in _registry:
        return _registry[key]

    with _registry_lock:

        if key in _registry:
            return _registry[key]

        if not os.path.exists(path):
            logger.info("[model_bundle] no trained model at %s (key=%s) - using heuristic fallback", path, key)
            _registry[key] = None
        else:
            try:
                bundle = joblib.load(path)
                _pin_all_candidates_inference_threads(bundle)
                bundle = prune_to_winner(bundle)
                _registry[key] = bundle
                _load_counts[key] += 1
            except Exception as exc:
                logger.warning("[model_bundle] failed to load %s (key=%s): %r - using heuristic fallback", path, key, exc)
                _registry[key] = None
        return _registry[key]

def get_load_counts() -> dict[str, int]:
    return dict(_load_counts)

def prune_to_winner(bundle: dict | None) -> dict | None:
    if bundle is None or not settings.AI_MODEL_LIGHT_MODE:
        return bundle
    candidates = bundle.get("models")
    if not isinstance(candidates, dict) or len(candidates) <= 1:
        return bundle
    trained_name = bundle.get("model_name")
    winner = candidates.get(trained_name)
    if winner is None:
        trained_name, winner = next(iter(candidates.items()))
    bundle["models"] = {trained_name: winner}
    bundle["model"] = winner
    bundle["model_name"] = trained_name
    logger.info(
        "[model_bundle] AI_MODEL_LIGHT_MODE on - dropped %d non-winning candidate(s), keeping only %r",
        len(candidates) - 1,
        trained_name,
    )
    return bundle
