"""Milestone 17 follow-up - startup model warm-up.

Each predictor module (crowd/delay/frequency) lazy-loads its .pkl
bundle via functools.lru_cache on first call, which is the right
default (a fresh deploy that hasn't been hit yet shouldn't pay any
model-load cost). The tradeoff: whichever request happens to be first
after a (re)start pays the full joblib.load() cost inline - roughly
1-1.5s per ~3MB bundle in this deployment, so up to ~3-4s if a
dashboard page fires crowd + delay + frequency calls together on that
first hit. That reads as "slow" or "stuck" to whoever's dashboard is
open at that moment, and in a multi-worker deployment it happens once
per worker process, not once total.

`warm_up_models()` just calls each module's cached loader once, up
front, during the FastAPI lifespan startup phase (see app/main.py) -
before the app starts accepting traffic. Same lru_cache instance, same
object, so this doesn't change any prediction behavior; it only moves
*when* the one-time load cost is paid. Errors are caught and logged,
never raised, so a missing/corrupt .pkl still falls back to the
heuristic path exactly as it already does per-module - it just won't
also block startup.
"""
import logging
import time

logger = logging.getLogger(__name__)


def warm_up_models() -> None:
    from app.ai_engine.prediction import crowd_predictor, delay_predictor, frequency_predictor

    for label, loader in (
        ("crowd_model", crowd_predictor._load_model),
        ("delay_model", delay_predictor._load_model),
        ("frequency_model", frequency_predictor._load_model),
    ):
        start = time.perf_counter()
        try:
            bundle = loader()
            elapsed = time.perf_counter() - start
            if bundle is None:
                logger.warning("[warmup] %s not found - heuristic fallback will serve requests", label)
            else:
                candidates = list((bundle.get("models") or {}).keys()) or [bundle.get("model_name", "?")]
                logger.info("[warmup] %s loaded in %.2fs (candidates: %s)", label, elapsed, candidates)
        except Exception as exc:
            logger.warning("[warmup] %s failed to load (%r) - heuristic fallback will serve requests", label, exc)
