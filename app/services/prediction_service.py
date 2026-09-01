from datetime import datetime, timedelta
from typing import Callable
import time

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.ai_engine.prediction.crowd_predictor import predict_crowd
from app.ai_engine.prediction.delay_predictor import predict_delay
from app.ai_engine.prediction.frequency_predictor import recommend_frequency
from app.core import cache
from app.enums.crowd_level import CrowdLevel
from app.enums.prediction_type import PredictionType
from app.models.prediction import Prediction
from app.models.station import Station

PREDICTION_CACHE_TTL_SECONDS = 300

PREDICTION_TTL = PREDICTION_CACHE_TTL_SECONDS

# Same network-wide median capacity fallback as
# app/database/seed_real_data.py's DEFAULT_CAPACITY - used only if a
# station somehow has no capacity value at all (should not happen in
# practice; every seeded station gets a real, derived capacity).
DEFAULT_CAPACITY_FALLBACK = 2400

# Phase 9: forecast_demand's `hours_ahead` had no upper bound - each
# unit runs a full crowd-prediction call (model inference, possibly a
# DB write via _save_prediction) inside the loop below, so an
# oversized value (e.g. hours_ahead=100000) is both an unbounded
# response (one Prediction object per hour) AND an unbounded amount of
# server-side compute/DB work per request, on an endpoint that's only
# rate-limited by request count (20/minute), not by request cost. One
# week of hourly forecasts is already generous for the "Demand
# Forecast" widget's actual use (frontend default is 6).
MAX_DEMAND_FORECAST_HOURS_AHEAD = 168

def _cached_prediction(cache_key: str, compute: Callable[[], dict]) -> dict:
    """Redis-first wrapper around a `predict_*`/`recommend_*` call.
    Fail-open like the rest of the app's caching (app/core/cache.py):
    a cache miss or a down Redis just falls through to actually
    running the model, same result either way, just slower."""
    cached = cache.get_json(cache_key)
    if cached is not None:
        if cached.get("target_datetime"):
            cached["target_datetime"] = datetime.fromisoformat(cached["target_datetime"])
        return cached

    result = compute()
    cache.set_json(cache_key, result, ttl_seconds=PREDICTION_CACHE_TTL_SECONDS)
    return result

def _cache_key_bucket(target_datetime: datetime) -> str:
    """Floor a target_datetime to the top of its hour, for cache-KEY
    purposes only (the value actually handed to the model is untouched
    - see each _cached_predict_*/_cached_recommend_* wrapper below).

    BUGFIX (repeated prediction calculations / incorrect cache keys):
    every predict_*/recommend_* model in app/ai_engine/prediction/ only
    ever reads `.hour` and `.weekday()` off its target_datetime -
    minutes/seconds/microseconds have zero effect on the features fed
    into the model (verified against crowd_predictor.py,
    delay_predictor.py and frequency_predictor.py). But the three
    wrappers below were keying their Redis cache on
    `target_datetime.isoformat()` built straight from
    `datetime.utcnow()` (forecast_crowd/forecast_delay/
    recommend_train_frequency/smart_recommendations/
    smart_recommendations_bulk all call these with "now") - a
    microsecond-precision timestamp that is different on every single
    call. That made PREDICTION_CACHE_TTL_SECONDS (5 minutes)
    meaningless for every "predict for right now" caller: two requests
    a millisecond apart (e.g. the AI Insights panel's
    smart_recommendations_bulk() firing on every ~10s live
    crowd/delay/station-alert WebSocket event, or several dashboard
    widgets independently calling forecast_crowd()/forecast_delay() on
    the same page load) always missed the cache and re-ran full model
    inference for an answer that was already sitting in Redis.

    traffic_pattern_analysis()/all_stations_traffic_pattern() already
    get this right - both floor `now` to the hour with
    `.replace(minute=0, second=0, microsecond=0)` before ever calling
    _cached_predict_crowd - this applies that same, already-established
    pattern to the cache KEY for the other four call sites too, instead
    of leaving them on the raw timestamp. The value passed to the
    actual predict_*/recommend_* call (and therefore the
    `target_datetime` returned/persisted on a cache MISS) is left
    exactly as before - only which Redis key a given call reads/writes
    changes."""
    return target_datetime.replace(minute=0, second=0, microsecond=0).isoformat()

def _crowd_cache_key(station_id: int, target_datetime: datetime, light: bool = False) -> str:
    return f"predict:crowd:{station_id}:{_cache_key_bucket(target_datetime)}:{light}"

def _delay_cache_key(station_id: int, target_datetime: datetime, train_id: int | None = None) -> str:
    return f"predict:delay:{station_id}:{_cache_key_bucket(target_datetime)}:{train_id}"

def _frequency_cache_key(station_id: int, target_datetime: datetime) -> str:
    return f"predict:frequency:{station_id}:{_cache_key_bucket(target_datetime)}"

def _cached_predict_crowd(station_id: int, target_datetime: datetime, light: bool = False) -> dict:
    key = _crowd_cache_key(station_id, target_datetime, light)
    return _cached_prediction(key, lambda: predict_crowd(station_id, target_datetime, light=light))

def _cached_predict_delay(db: Session, station_id: int, target_datetime: datetime, train_id: int | None = None) -> dict:
                                                                     
    key = _delay_cache_key(station_id, target_datetime, train_id)
    return _cached_prediction(key, lambda: predict_delay(station_id, target_datetime, train_id=train_id, db=db))

def _cached_recommend_frequency(station_id: int, target_datetime: datetime) -> dict:
    key = _frequency_cache_key(station_id, target_datetime)
    return _cached_prediction(key, lambda: recommend_frequency(station_id, target_datetime))

def _save_prediction(
    db: Session,
    station_id: int,
    prediction_type: PredictionType,
    predicted_value: float,
    confidence: float,
    target_datetime: datetime,
    model_version: str,
    commit: bool = True,
) -> Prediction:
    record = Prediction(
        station_id=station_id,
        prediction_type=prediction_type,
        predicted_value=predicted_value,
        predicted_count=round(predicted_value) if prediction_type in (
            PredictionType.CROWD, PredictionType.DEMAND
        ) else None,
        confidence=confidence,
        target_datetime=target_datetime,
        model_version=model_version,
    )
    db.add(record)
                                                                   
    if commit:
        db.commit()
        db.refresh(record)
    return record

PREDICTION_WRITE_DEDUPE_SECONDS = PREDICTION_CACHE_TTL_SECONDS

def _get_or_save_prediction(
    db: Session,
    *,
    station_id: int,
    prediction_type: PredictionType,
    predicted_value: float,
    confidence: float,
    target_datetime: datetime,
    model_version: str,
    commit: bool = True,
) -> Prediction:
    """BUGFIX (duplicate prediction records): forecast_crowd(),
    forecast_demand(), forecast_delay() and recommend_train_frequency()
    used to call `_save_prediction` unconditionally on every call,
    INSERTing a brand-new row even when `_cached_predict_*`/
    `_cached_recommend_*` served the answer straight out of the value
    cache (`PREDICTION_CACHE_TTL_SECONDS` = 5 minutes) without running
    the model at all - the exact same class of bug already fixed for
    smart_recommendations()/smart_recommendations_bulk() via
    `_maybe_persist_recommendation_predictions` (see its docstring).

    An earlier version of this fix decided whether to write by peeking
    the value cache (`cache.get_json`) BEFORE calling
    `_cached_predict_*`, then writing only if that peek missed. That is
    a classic check-then-act race: two requests for the same
    (station_id, prediction_type, target_datetime) arriving close
    together can both peek before either one's `_cached_prediction`
    call has finished populating the cache, so both see "not cached
    yet" and both go on to INSERT - a genuine duplicate row despite the
    dedup logic, indistinguishable from the original bug at the DB
    level. Two rows genuinely written from two truly independent
    predictions (different target_datetime, e.g. two different hours)
    are correct and expected - historical analytics depends on every
    real prediction being kept - the bug is only ever two rows for the
    SAME (station, type, target_datetime).

    Fixed the same way `_maybe_persist_recommendation_predictions`
    already does it: `cache.set_nx` is an atomic Redis `SET ... NX EX`
    - only ONE concurrent caller can ever win it for a given key, so
    "should I write?" and "am I the one writing?" are now the same
    atomic operation instead of two separate steps with a window
    between them. The loser doesn't just skip - since forecast_crowd()
    etc. return the Prediction itself as their response, it looks up
    and returns the row the winner just wrote (retrying briefly in case
    it hasn't committed yet), so a request never comes back with a
    missing record. A genuine recompute always carries a fresh,
    never-before-seen target_datetime (derived from datetime.utcnow()
    at model-call time), so it can never collide with an older key and
    always wins its own write - nothing about a real, distinct
    prediction is ever suppressed or lost."""
    dedupe_key = (
        f"predictions:write:{prediction_type.value}:{station_id}:"
        f"{target_datetime.isoformat() if hasattr(target_datetime, 'isoformat') else target_datetime}"
    )

    if cache.set_nx(dedupe_key, PREDICTION_WRITE_DEDUPE_SECONDS):
        return _save_prediction(
            db,
            station_id=station_id,
            prediction_type=prediction_type,
            predicted_value=predicted_value,
            confidence=confidence,
            target_datetime=target_datetime,
            model_version=model_version,
            commit=commit,
        )

    # Someone else already won the write for this exact key - find the
    # row they wrote instead of inserting a duplicate. A short retry
    # loop covers the narrow window where the winner hasn't committed
    # yet (this function may be called with commit=False and batched by
    # its caller - see forecast_demand); if it never shows up (e.g. the
    # winner's transaction rolled back), fall back to writing ourselves
    # so this caller's response/analytics record is never silently lost.
    for attempt in range(3):
        existing = (
            db.query(Prediction)
            .filter(
                Prediction.station_id == station_id,
                Prediction.prediction_type == prediction_type,
                Prediction.target_datetime == target_datetime,
            )
            .order_by(Prediction.created_at.desc())
            .first()
        )
        if existing is not None:
            return existing
        if attempt < 2:
            time.sleep(0.05)

    return _save_prediction(
        db,
        station_id=station_id,
        prediction_type=prediction_type,
        predicted_value=predicted_value,
        confidence=confidence,
        target_datetime=target_datetime,
        model_version=model_version,
        commit=commit,
    )

def _require_station(db: Session, station_id: int) -> Station:
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")
    return station

def forecast_crowd(db: Session, station_id: int, target_datetime: datetime | None = None) -> Prediction:
    """Crowd prediction models."""
    _require_station(db, station_id)
    dt = target_datetime or datetime.utcnow()
    result = _cached_predict_crowd(station_id, dt)
    record = _get_or_save_prediction(
        db,
        station_id=station_id,
        prediction_type=PredictionType.CROWD,
        predicted_value=result["predicted_count"],
        confidence=result["confidence"],
        target_datetime=result["target_datetime"],
        model_version=result["model_version"],
    )
    # Per-candidate breakdown (random_forest/xgboost) isn't a DB
    # column - Prediction only ever stores the winning model's value -
    # so it's attached to the already-saved/refreshed record here,
    # purely for this response, instead of being silently dropped.
    record.models = result.get("models", {})
    return record

def forecast_demand(db: Session, station_id: int, hours_ahead: int = 6) -> list[Prediction]:
    """Passenger demand forecasting: hour-by-hour for the next N hours.

    `hours_ahead` is clamped to [1, MAX_DEMAND_FORECAST_HOURS_AHEAD] -
    see that constant's comment above for why."""
    _require_station(db, station_id)
    hours_ahead = min(max(hours_ahead or 1, 1), MAX_DEMAND_FORECAST_HOURS_AHEAD)
    now = datetime.utcnow()
    records = []
    for i in range(1, hours_ahead + 1):
        target = now + timedelta(hours=i)
        result = _cached_predict_crowd(station_id, target)
        record = _get_or_save_prediction(
            db,
            station_id=station_id,
            prediction_type=PredictionType.DEMAND,
            predicted_value=result["predicted_count"],
            confidence=result["confidence"],
            target_datetime=result["target_datetime"],
            model_version=result["model_version"],
            commit=False,
        )
        records.append(record)
                                                                
    db.commit()
    for record in records:
        db.refresh(record)
    return records

def forecast_delay(db: Session, train_id: int, station_id: int) -> Prediction:
    """Delay impact prediction, feeding the Scheduling Management Module."""
    _require_station(db, station_id)
    dt = datetime.utcnow()
    result = _cached_predict_delay(db, station_id, dt, train_id=train_id)
    record = _get_or_save_prediction(
        db,
        station_id=station_id,
        prediction_type=PredictionType.DELAY,
        predicted_value=result["predicted_delay_minutes"],
        confidence=0.7,
        target_datetime=result["target_datetime"],
        model_version=result["model_version"],
    )
    # Same as forecast_crowd above: attach the random_forest/xgboost
    # breakdown that predict_delay() already computes but that has no
    # DB column to live in, so the API response can carry it too.
    record.models = result.get("models", {})
    return record

def recommend_train_frequency(db: Session, station_id: int, is_peak_hour: bool = False) -> Prediction:
    """Train frequency recommendations / resource utilization optimization."""
    _require_station(db, station_id)
    target = datetime.utcnow()
    if is_peak_hour:
        target = target.replace(hour=9)                                  
    result = _cached_recommend_frequency(station_id, target)
    record = _get_or_save_prediction(
        db,
        station_id=station_id,
        prediction_type=PredictionType.FREQUENCY,
        predicted_value=result["recommended_frequency_minutes"],
        confidence=0.75,
        target_datetime=result["target_datetime"],
        model_version=result["model_version"],
    )
    # Same as forecast_crowd above: attach the random_forest/xgboost
    # breakdown that recommend_frequency() already computes but that
    # has no DB column to live in, so the API response can carry it too.
    record.models = result.get("models", {})
    return record

def traffic_pattern_analysis(db: Session, station_id: int) -> dict:
    """Traffic pattern analysis: 24h predicted demand curve for a station."""
    _require_station(db, station_id)
    now = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    curve = []
    for hour in range(24):
        target = now.replace(hour=hour)
                                                                       
        result = _cached_predict_crowd(station_id, target, light=True)
        curve.append({
            "hour": hour,
            "predicted_count": result["predicted_count"],
            "is_peak_hour": 8 <= hour <= 11 or 17 <= hour <= 20,
        })

    peak = max(curve, key=lambda c: c["predicted_count"])
    trough = min(curve, key=lambda c: c["predicted_count"])

    return {
        "station_id": station_id,
        "hourly_forecast": curve,
        "peak_hour": peak["hour"],
        "peak_predicted_count": peak["predicted_count"],
        "quietest_hour": trough["hour"],
    }

def all_stations_traffic_pattern(db: Session, state: str | None = None) -> dict:
    """Same 24h predicted-demand curve as traffic_pattern_analysis, but
    summed across every station (optionally scoped to `state`) in ONE
    call, instead of the caller having to fan out a separate request
    per station.

    This exists because the "Passenger Analytics" dashboard widget
    needs an all-stations total, and was previously built by having
    the FRONTEND call GET /traffic-pattern/{station_id} once per
    station via Promise.all() - one HTTP request, one DB round trip,
    and 24 model lookups PER STATION, all fired in parallel on every
    page load. Two compounding problems with that:

    1. This endpoint is rate-limited at 20/minute per IP (see the
       module docstring in api/v1/prediction.py - deliberate, since
       every call here can run an ML model). A city with more than
       ~20 stations blew straight through that limit on a single page
       load, so most of those per-station requests came back 429 and
       the widget surfaced "Couldn't load the forecast right now" -
       not because the prediction service was actually slow or down,
       but because the page itself DDoS'd its own rate limiter.
    2. Even the requests that didn't get rate-limited meant dozens of
       concurrent DB session / ML calls competing for the same
       process, which is what made the ones that DID succeed take
       far longer than a single request should.

    Computing the sum server-side keeps the exact same per-station,
    per-hour Redis-cached prediction calls (so the actual compute cost
    doesn't change and every value stays independently verifiable),
    but collapses N HTTP requests and N rate-limit hits into 1.
    """
                                                                   
    cache_key = f"predict:traffic-pattern-aggregate:{state or 'all'}"
    cached = cache.get_json(cache_key)
    if cached is not None:
        return cached

    from app.ai_engine.prediction.crowd_predictor import predict_crowd_bulk
    from app.utils.geo import cities_for_state

    cities = cities_for_state(state)
    query = db.query(Station)
    if cities:
        query = query.filter(Station.city.in_(cities))
    stations = query.all()

    now = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    station_ids = [s.id for s in stations]
    hours = list(range(24))

    # One vectorized model call for every (station, hour) pair instead
    # of 24 x len(stations) individual predict_crowd() calls in a loop
    # (see predict_crowd_bulk's docstring) - this is what was making
    # this endpoint slow enough to time out on a cache-cold request.
    bulk_results = predict_crowd_bulk(station_ids, hours, now)

    totals = [0] * 24
    for station_id in station_ids:
        for hour in hours:
            totals[hour] += bulk_results[(station_id, hour)]["predicted_count"]

    curve = [
        {
            "hour": hour,
            "predicted_count": totals[hour],
            "is_peak_hour": 8 <= hour <= 11 or 17 <= hour <= 20,
        }
        for hour in range(24)
    ]
    peak = max(curve, key=lambda c: c["predicted_count"])
    trough = min(curve, key=lambda c: c["predicted_count"])

    response = {
        "station_count": len(stations),
        "hourly_forecast": curve,
        "peak_hour": peak["hour"],
        "peak_predicted_count": peak["predicted_count"],
        "quietest_hour": trough["hour"],
    }
    cache.set_json(cache_key, response, ttl_seconds=PREDICTION_CACHE_TTL_SECONDS)
    return response

# Phase 1 (P0-1 / P2-3) fix. Two independent problems used to compound
# here:
#
#   1. The frontend (AIInsights.tsx) fanned out one HTTP GET per
#      station - up to MAX_STATIONS_ANALYZED=15 of them, in parallel,
#      on every dashboard load AND on every single live `crowd_update`/
#      `delay_alert`/`station_alert` WebSocket event (roughly once per
#      SIMULATOR_INTERVAL_SECONDS). See smart_recommendations_bulk()
#      below and AIInsights.tsx for the frontend half of this fix.
#   2. Independent of #1: every single call to this function - even a
#      pure cache HIT that recomputed nothing - unconditionally wrote 3
#      new `Prediction` rows and committed. That meant DB writes scaled
#      with *dashboard views*, not with *actual new predictions
#      computed*, so the row count grew unbounded purely from people
#      looking at the AI Insights panel.
#
# RECOMMENDATION_WRITE_DEDUPE_SECONDS decouples "record recent AI
# activity for the Activity Timeline widget" from "write on every
# request": a station's 3 predictions are persisted at most once per
# window, no matter how many times smart_recommendations()/
# smart_recommendations_bulk() is called for it in between - by a
# single dashboard tab refreshing, several tabs open at once, or the
# bulk endpoint being hit repeatedly. This is a separate Redis key from
# PREDICTION_CACHE_TTL_SECONDS (the *value* cache) - a value-cache hit
# and a write-dedupe hit are different questions ("do I need to
# recompute?" vs "do I need to record this?").
RECOMMENDATION_WRITE_DEDUPE_SECONDS = 60

def _maybe_persist_recommendation_predictions(
    db: Session, station_id: int, crowd: dict, delay: dict, frequency: dict,
) -> None:
    """Persist the crowd/delay/frequency predictions backing a
    smart-recommendations call, but at most once per station per
    RECOMMENDATION_WRITE_DEDUPE_SECONDS - see the module-level comment
    above (Phase 1, P2-3) for why this guard exists."""
    dedupe_key = f"predictions:reco-write:{station_id}"
    if not cache.set_nx(dedupe_key, RECOMMENDATION_WRITE_DEDUPE_SECONDS):
        return

    _save_prediction(
        db,
        station_id=station_id,
        prediction_type=PredictionType.CROWD,
        predicted_value=crowd["predicted_count"],
        confidence=crowd["confidence"],
        target_datetime=crowd["target_datetime"],
        model_version=crowd["model_version"],
        commit=False,
    )
    _save_prediction(
        db,
        station_id=station_id,
        prediction_type=PredictionType.DELAY,
        predicted_value=delay["predicted_delay_minutes"],
        confidence=0.7,
        target_datetime=delay["target_datetime"],
        model_version=delay["model_version"],
        commit=False,
    )
    _save_prediction(
        db,
        station_id=station_id,
        prediction_type=PredictionType.FREQUENCY,
        predicted_value=frequency["recommended_frequency_minutes"],
        confidence=0.75,
        target_datetime=frequency["target_datetime"],
        model_version=frequency["model_version"],
        commit=False,
    )
    db.commit()


# Phase 3 fix (docs/crowd-data-correctness.md, Bug 5). This used to be a flat
# `crowd["predicted_count"] > 800` for EVERY station regardless of that
# station's actual scale - a small station whose real hourly throughput
# never goes much above 200 would never fire, while a large interchange
# whose normal throughput comfortably exceeds 800 would be flagged
# "high crowd" constantly, alert-fatigue style.
#
# `crowd["predicted_count"]` is the crowd model's predicted hourly
# throughput (entries + exits) - the same quantity the real dataset's
# own `crowding_index` column is built from (crowding_index =
# (entries + exits) / capacity - verified directly against
# passenger_flow.csv.gz, see docs/crowd-data-correctness.md). So the
# apples-to-apples, per-station-scaled threshold is the SAME
# capacity-relative ratio the dataset itself uses, calibrated against
# its own "Crowded" cutoff (0.35 - see app/enums/crowd_level.py's
# MODERATE_MAX_RATIO, measured off the same column). A predicted
# throughput at or above 35% of a station's capacity is exactly what
# the dataset itself would label "Crowded" or worse for that station.
HIGH_CROWD_THROUGHPUT_RATIO = 0.35

def _build_recommendations(
    station_id: int, crowd: dict, delay: dict, frequency: dict, capacity: int | None = None,
) -> list[dict]:
    """Pure formatting: turn a (crowd, delay, frequency) prediction
    triple into the list of recommendation cards the frontend renders.
    Extracted out of smart_recommendations() so smart_recommendations_bulk()
    can reuse the exact same rules for many stations without duplicating
    them (and without re-fetching/re-persisting predictions itself).

    `capacity` is the station's real, per-station capacity (see
    docs/crowd-data-correctness.md Bug 2) - callers should always pass it;
    it's optional only so this stays testable/callable in isolation.
    When omitted, DEFAULT_CAPACITY_FALLBACK is used so the ratio-based
    check still degrades gracefully instead of throwing.
    """
    recommendations = []
    effective_capacity = capacity or DEFAULT_CAPACITY_FALLBACK
    crowd_ratio = crowd["predicted_count"] / effective_capacity if effective_capacity else 0

    if crowd_ratio > HIGH_CROWD_THROUGHPUT_RATIO:
        recommendations.append({
            "station_id": station_id,
            "title": "High crowd expected",
            "detail": f"Predicted ~{crowd['predicted_count']} passengers "
                      f"({crowd_ratio * 100:.0f}% of this station's capacity). "
                      f"Consider deploying additional staff and opening extra gates.",
            "severity": "warning",
        })

    if delay["predicted_delay_minutes"] > 3:
        recommendations.append({
            "station_id": station_id,
            "title": "Delay risk",
            "detail": f"Model predicts ~{delay['predicted_delay_minutes']} min of delay "
                      f"driven by current congestion levels.",
            "severity": "warning",
        })

    recommendations.append({
        "station_id": station_id,
        "title": "Frequency suggestion",
        "detail": f"Recommended train interval: {frequency['recommended_frequency_minutes']} min "
                  f"({'peak' if frequency['is_peak_hour'] else 'off-peak'} slot).",
        "severity": "info",
    })

    if not recommendations:
        recommendations.append({
            "station_id": station_id,
            "title": "Normal operations",
            "detail": "No anomalies detected; current schedule and staffing look adequate.",
            "severity": "info",
        })

    return recommendations

def smart_recommendations(db: Session, station_id: int) -> list[dict]:
    """Smart recommendations combining crowd + delay + frequency predictions
    for a single station. Kept for callers that only ever need one
    station; see smart_recommendations_bulk() for the many-stations-in-
    one-call version the dashboard actually uses now."""
    station = _require_station(db, station_id)
    dt = datetime.utcnow()
    crowd = _cached_predict_crowd(station_id, dt)
    delay = _cached_predict_delay(db, station_id, dt)
    frequency = _cached_recommend_frequency(station_id, dt)

    _maybe_persist_recommendation_predictions(db, station_id, crowd, delay, frequency)

    return _build_recommendations(station_id, crowd, delay, frequency, capacity=station.capacity)

def smart_recommendations_bulk(db: Session, station_ids: list[int]) -> dict[int, list[dict]]:
    """Same recommendations as smart_recommendations(), for many
    stations in ONE call - the exact pattern already used by
    all_stations_traffic_pattern() (see its docstring) to fix the
    identical class of problem for the traffic-pattern widget, applied
    here to the AI Insights panel (Phase 1, P0-1).

    Previously, AIInsights.tsx ranked up to MAX_STATIONS_ANALYZED=15
    stations by occupancy and fired 15 parallel
    `GET /predictions/recommendations/{station_id}` calls via
    Promise.all() - on every dashboard load AND on every single live
    crowd/delay/station-alert WebSocket event (roughly every
    SIMULATOR_INTERVAL_SECONDS=10s). That's up to 90 rate-limited calls
    per minute from a single browser tab against an endpoint capped at
    20/minute per IP, so most of them came back 429, and (independent
    of the 429s) every *successful* call still wrote 3 Prediction rows
    per the pre-fix version of _save_prediction usage above.

    Collapsing this to one endpoint means: 1 rate-limit slot consumed
    per refresh instead of up to 15, and the per-station cached
    crowd/delay/frequency lookups (_cached_predict_crowd etc.) are
    still reused across stations exactly as before - no compute or
    caching behaviour changes per station, only the transport (1 HTTP
    round trip instead of N) and the write path (see
    _maybe_persist_recommendation_predictions above) change.
    """
    dt = datetime.utcnow()
    # One query for every station's capacity instead of one per
    # station in the loop below (same N+1-avoidance pattern already
    # used elsewhere in this file/module - e.g. predict_crowd_bulk).
    capacities = dict(
        db.query(Station.id, Station.capacity)
        .filter(Station.id.in_(station_ids))
        .all()
    )
    results: dict[int, list[dict]] = {}
    for station_id in station_ids:
        crowd = _cached_predict_crowd(station_id, dt)
        delay = _cached_predict_delay(db, station_id, dt)
        frequency = _cached_recommend_frequency(station_id, dt)

        _maybe_persist_recommendation_predictions(db, station_id, crowd, delay, frequency)

        results[station_id] = _build_recommendations(
            station_id, crowd, delay, frequency, capacity=capacities.get(station_id),
        )
    return results

def get_crowd_model_metrics() -> dict:
    """New: live evaluation metrics for the production crowd/demand model
    (same trained model backs both - see forecast_demand above),
    computed from the real passenger_flow.csv held-out test split. Powers
    the AI Prediction dashboard page."""
    from app.ai_engine.prediction.crowd_metrics import compute_crowd_metrics

    return compute_crowd_metrics()

def get_delay_model_metrics() -> dict:
    """New: live evaluation metrics for the production delay model,
    computed from the real train_operations.csv held-out test split.
    Same role as get_crowd_model_metrics above; powers the delay
    section of the AI Prediction dashboard page."""
    from app.ai_engine.prediction.delay_metrics import compute_delay_metrics

    return compute_delay_metrics()

def get_frequency_model_metrics() -> dict:
    """New: live evaluation metrics for the production train-frequency
    recommendation model, computed from the real passenger_flow.csv
    held-out test split. Same role as get_crowd_model_metrics above;
    powers the frequency section of the AI Prediction dashboard page."""
    from app.ai_engine.prediction.frequency_metrics import compute_frequency_metrics

    return compute_frequency_metrics()