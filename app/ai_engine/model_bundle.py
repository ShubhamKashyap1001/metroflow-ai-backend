"""Shared helper for loading a predictor's .pkl bundle in a memory-
conscious way.

Each of the 3 saved bundles (crowd/delay/frequency) stores TWO fully-
trained models under bundle["models"] - "random_forest" and "xgboost"
- so the dashboard can show both predictions side by side (see
predict_crowd's docstring in app/ai_engine/prediction/crowd_predictor.py).
That's a nice feature, but it means every bundle costs roughly double
the memory of just its winning model once unpickled - a real problem
on a 512MB instance running all 3 bundles at once.

When settings.AI_MODEL_LIGHT_MODE is True (the default - see
app/core/config.py), this trims a freshly-loaded bundle down to just
its winning model immediately after joblib.load(), before the caller's
own @lru_cache(maxsize=1) ever holds onto it. The bundle's shape
(model/model_name/features/models keys) is left exactly as the
predictor modules expect - `models` just ends up with a single entry
instead of two - so no other code has to change to support this.
"""
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)


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
