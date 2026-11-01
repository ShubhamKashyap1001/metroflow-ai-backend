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
