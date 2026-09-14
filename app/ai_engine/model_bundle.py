"""Shared helper for loading a predictor's .pkl bundle in a memory-
conscious way.

Bundle shape: every saved crowd/delay/frequency bundle is a dict with
at least `model` (the fitted estimator), `model_name` (str) and
`features` (ordered list of column names the estimator expects).
Some bundles additionally carry a `models` dict of every trained
candidate ("random_forest"/"xgboost" style keys) so the dashboard can
show more than one prediction side by side (see predict_crowd's
docstring in app/ai_engine/prediction/crowd_predictor.py) - the current
production artifacts (Sept 2026 retrain) only ship the single winning
model and omit `models` entirely, which every call site already treats
as equivalent to a one-entry `models` dict (`bundle.get("models") or
{trained_name: bundle["model"]}`), so both bundle shapes work
unchanged. When a bundle DOES carry multiple candidates, keeping all
of them loaded costs roughly double the memory of just the winner once
unpickled - a real problem on a 512MB instance running all 3 bundles
at once.

When settings.AI_MODEL_LIGHT_MODE is True (the default - see
app/core/config.py), this trims a freshly-loaded bundle down to just
its winning model immediately after joblib.load(), before it's ever
stored in the registry below. The bundle's shape (model/model_name/
features/models keys) is left exactly as the predictor modules expect
- `models`, if present, just ends up with a single entry instead of
several - so no other code has to change to support this. For a
bundle that never had a `models` dict in the first place (the current
production artifacts), this is a no-op.

get_or_load() below is the actual single-instance-per-process
registry: a small fixed dict keyed by "crowd"/"delay"/"frequency"
(never grows beyond those 3 keys), guarded by a lock so concurrent
first-time callers can't each end up running their own joblib.load()
for the same key. Each predictor module's own `_load_model()` is now a
thin wrapper around this, kept so existing callers (app/ai_engine/
warmup.py, each predictor's own predict_* functions) don't have to
change.

CPU note: every model here is saved with `n_jobs=-1` (its training-time
default). get_or_load() pins this to 1 thread right after
joblib.load() (see _pin_inference_threads/_pin_all_candidates_
inference_threads below) so a single-row prediction on this
WEB_CONCURRENCY=1 instance never spins up a thread pool sized to the
host's total CPU count - pure overhead for one row, never a change to
the model's learned weights or predictions. For XGBoost's crowd/
frequency models this pin has to be applied to the underlying native
Booster's `nthread` param directly (not just the sklearn wrapper's
`n_jobs` attribute, which XGBoost silently ignores post-unpickle) -
see the long comment on _pin_inference_threads for why.
"""
import logging
import os
import threading

import joblib

from app.core.config import settings

logger = logging.getLogger(__name__)


class ModelFeatureContractError(RuntimeError):
    """Raised when a loaded bundle's `features` list contains a name the
    calling predictor doesn't know how to supply a value for.

    This is a deliberate, explicit failure instead of silently trusting
    dict/DataFrame column ordering: each predictor already builds its
    inference row by looking up every name in `bundle["features"]`
    against a known set of inputs it can compute, so an unrecognized
    name means the saved model's feature contract has drifted from what
    this backend build knows how to feed it (e.g. a retrain added a
    new feature the predictor was never updated to compute). Callers
    catch this the same way they already catch any other predict-time
    failure - see each predictor's `except Exception` block - so the
    request degrades to that predictor's heuristic fallback and gets
    logged, instead of either crashing or silently mislabeling
    columns.
    """


def validate_feature_contract(bundle_features: list[str], known_features: set[str], *, context: str) -> None:
    """Raise ModelFeatureContractError if `bundle_features` (the exact,
    ordered column list saved inside the .pkl) names anything outside
    `known_features` (everything this predictor actually knows how to
    compute a value for). Called by each predictor before it constructs
    its inference DataFrame - see crowd_predictor.predict_crowd,
    delay_predictor.predict_delay, frequency_predictor.recommend_frequency.
    """
    unknown = [f for f in bundle_features if f not in known_features]
    if unknown:
        raise ModelFeatureContractError(
            f"{context}: model bundle expects unknown feature(s) {unknown!r} "
            f"(known: {sorted(known_features)!r})"
        )


# Fixed, bounded set of model slots this registry will ever hold - one
# instance each for crowd/delay/frequency, never more.
_VALID_KEYS = ("crowd", "delay", "frequency")

# Guards both the registry dict below AND the load counters. A single
# lock (rather than one per key) keeps this simple, and is cheap here:
# it's only ever held for the duration of a joblib.load() on a cold
# cache, which happens at most once per key for the life of the
# process - every later call (the overwhelming majority) hits the
# lock-free fast path below and never blocks on it.
_registry_lock = threading.Lock()
_registry: dict[str, dict | None] = {}

# Debug-only counters (see get_load_counts()) - how many times
# joblib.load() has actually run for each key. Used by tests to assert
# "loaded exactly once even under concurrent requests"; not logged
# verbosely in production, just incremented.
_load_counts: dict[str, int] = {key: 0 for key in _VALID_KEYS}


def _pin_inference_threads(model) -> None:
    """Force `n_jobs=1` on a fitted estimator that exposes it, so a
    single `.predict()` call at request time never spins up a thread
    pool sized to the HOST's total logical CPUs.

    Both RandomForestRegressor and XGBRegressor default to `n_jobs=-1`
    at training time (see the saved bundles' pickled params), which is
    reasonable for training but wasteful for inference here: this is a
    Render Free instance running with WEB_CONCURRENCY=1 (effectively
    one usable core for this process), and every prediction request is
    a single row - there is nothing to meaningfully parallelize, only
    thread-pool creation/teardown overhead paid on every call, which
    burns CPU (and briefly extra RAM for those threads) for no benefit.

    This only changes a runtime execution knob, not anything the model
    "learned" - it does NOT retrain the model, does not touch its
    weights/trees/hyperparameters, and does not change what a
    prediction returns. Silently skipped for any estimator that
    doesn't expose `n_jobs` (nothing to change).

    IMPORTANT (XGBoost-specific): setting `model.n_jobs = 1` on an
    already-unpickled XGBRegressor/XGBClassifier is a no-op for the
    actual native thread count. The sklearn wrapper's `n_jobs`
    attribute is only read by XGBoost when a Booster is first built
    (i.e. at `.fit()` time); a Booster loaded from a pickle already
    has its own `nthread` baked into its native (C++) config (saved
    here as `-1` = "let OpenMP use every core it can see"), and
    `.predict()` on the wrapper does not push the Python attribute
    back down into that existing Booster. Verified directly: after
    `model.n_jobs = 1`, `model.get_booster().save_config()` still
    reports `nthread: -1`, and a subsequent `.predict()` does not
    change that. Left alone, this means every crowd/frequency
    prediction spins up a native OpenMP worker-thread pool sized to
    whatever core count the container reports (which on a Render Free
    instance is often the host's full vCPU count, not the fractional
    CPU actually granted) - each extra thread's stack is memory that
    is never released for the life of the process. This is real,
    unnecessary XGBoost thread/worker allocation, distinct from (and
    not fixed by) the sklearn-style `n_jobs` pin above, which only
    round-trips correctly for estimators like RandomForestRegressor
    that re-read `self.n_jobs` on every `.predict()` via
    `joblib.Parallel`.

    The fix below pins the native Booster's `nthread` directly via
    `set_param()`, which - unlike the wrapper attribute - does take
    effect immediately (verified: `save_config()` reports `nthread: 1`
    right after this call). This changes nothing about tree structure,
    weights, or split logic, so predictions are bit-identical
    (verified: same input produces the same output before and after
    pinning). Skipped for any estimator without `get_booster()`
    (nothing to change for RandomForest and friends).
    """
    if hasattr(model, "n_jobs"):
        try:
            model.n_jobs = 1
        except Exception:
            # Never let a cosmetic thread-count tweak block a model
            # from loading - worst case it just keeps its trained
            # default n_jobs.
            pass

    get_booster = getattr(model, "get_booster", None)
    if callable(get_booster):
        try:
            get_booster().set_param({"nthread": 1})
        except Exception:
            # Same defensive stance as above: worst case the native
            # Booster keeps its trained-time nthread default.
            pass


def _pin_all_candidates_inference_threads(bundle: dict | None) -> None:
    if not bundle:
        return
    _pin_inference_threads(bundle.get("model"))
    for candidate in (bundle.get("models") or {}).values():
        _pin_inference_threads(candidate)


def get_or_load(key: str, path: str):
    """Thread-safe lazy singleton loader for one of the 3 fixed model
    bundles. Returns the same cached instance (or the same cached
    `None`, if the .pkl is missing/corrupt) on every call for a given
    `key` - the underlying joblib.load() runs at most once per key per
    process, even if multiple requests call this for the same
    not-yet-loaded key at nearly the same time (double-checked
    locking: the lock is only taken on a cache miss, and re-checked
    once held, so a thread that loses the race to another thread just
    reuses what that thread loaded instead of loading its own copy).
    """
    if key not in _VALID_KEYS:
        raise ValueError(f"unknown model key {key!r} - expected one of {_VALID_KEYS}")

    # Fast path: already loaded (the common case - every call after
    # the first one for this key lands here without ever touching the
    # lock).
    if key in _registry:
        return _registry[key]

    with _registry_lock:
        # Re-check: another thread may have loaded (or failed to load)
        # this key while we were waiting for the lock.
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
    """Debug/test helper: how many times joblib.load() has actually
    executed for each model key so far in this process. Not used by
    any production code path."""
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
