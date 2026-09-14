# MetroFlow AI — Backend

AI-powered metro operations platform: live train tracking, crowd/demand monitoring, delay & frequency prediction, alerts, and a passenger-facing chatbot — built as a single FastAPI service backed by PostgreSQL and Redis.

---

## 1. Tech stack

| Layer | Technology |
|---|---|
| API framework | FastAPI (async), Uvicorn |
| Database | PostgreSQL, SQLAlchemy 2.0 ORM, Alembic migrations |
| Cache / pub-sub | Redis (optional — app degrades gracefully if unavailable) |
| Auth | Supabase (JWT verification only — no passwords touch this backend) |
| ML | scikit-learn (RandomForest) + XGBoost, joblib-serialized bundles |
| Realtime | Native WebSocket (`/ws/monitor`), Redis pub/sub relay across workers |
| Chatbot | Google Gemini API (`gemini-3.5-flash`), tool-calling against real DB data |
| Background jobs | asyncio tasks, Redis-lease leader election for multi-worker safety |
| Rate limiting | slowapi |
| Metrics | prometheus-client (`/metrics`) |

## 2. High-level architecture

```
                         ┌─────────────────────────┐
                         │   Next.js Frontend       │
                         └────────────┬─────────────┘
                       REST (/api/v1)  │  WebSocket (/ws/monitor)
                                       ▼
┌──────────────────────────────────────────────────────────────────┐
│                          FastAPI app (main.py)                    │
│                                                                     │
│  Routers: auth, users, stations, trains, crowd, checkin/checkout, │
│  schedules, predictions, analytics, alerts, enquiries, news,       │
│  notifications, meta, admin, chatbot, health                       │
│                                                                     │
│  Background loops (leader-elected — only ONE process runs each,   │
│  even with WEB_CONCURRENCY > 1 or multiple replicas):              │
│    • Simulator  (advances live crowd/train state every tick)      │
│    • Train tracker (moves trains along their routes)               │
│    • Crowd-log retention / rollup                                  │
│    • Notification-bin retention                                    │
│                                                                     │
│  ML layer (app/ai_engine): 3 predictors, lazy-loaded & cached      │
└───────────┬───────────────────────┬────────────────────┬──────────┘
            │                       │                    │
            ▼                       ▼                    ▼
      PostgreSQL              Redis (cache,        Gemini API
   (21 tables, Alembic)    leader-election lease,   (chatbot)
                             pub/sub relay,
                             response cache)
```

## 3. Machine-learning layer — how prediction actually works

MetroFlow ships **3 independent predictors**, each trained offline (see `colab_training/`) and shipped as a pre-trained `.pkl` bundle in `app/ai_engine/saved_models/`:

| Model | File | Predicts | Input features (examples) |
|---|---|---|---|
| **Crowd** | `crowd_model.pkl` | Station crowd/demand level for a given hour | station, hour, day type, historical average, time-of-day features |
| **Delay** | `delay_model.pkl` | Expected train delay (minutes) | route, scheduled time, `capacity_passengers` (real per-train DB column), `train_age_days` (real, from `commissioned_date`), historical delay patterns |
| **Frequency** | `frequency_model.pkl` | Recommended service frequency | station demand, peak-hour flag, current headway |

### 3.1 Bundle format

Every `.pkl` is a dict:
```python
{
  "model": <the winning fitted estimator>,
  "model_name": "random_forest" | "xgboost",
  "features": [...],       # exact ordered column names the model expects
  "models": {...},         # optional — both candidates, for side-by-side comparison
  "metrics": {...}         # MAE etc. from offline evaluation
}
```
Both a **RandomForest** and an **XGBoost** candidate are trained for each target; the one with the lowest MAE is picked as `"model"`. The dashboard's "compare models" view reads `models` (or the metrics modules, which independently re-evaluate both) so operators can see both predictions side by side even though production inference only ever uses the winner.

### 3.2 Loading strategy (`app/ai_engine/model_bundle.py`)

This is the part that got the most engineering attention, because the app is designed to run on a **512MB free-tier instance (Render Free)**:

- **`get_or_load()`** is a process-wide, lock-guarded registry keyed by `"crowd"`/`"delay"`/`"frequency"` — never grows beyond 3 entries, and concurrent first-time callers don't each trigger their own `joblib.load()`.
- **`AI_EAGER_WARMUP`** (default `False`): if `True`, all 3 bundles load at startup (in a worker thread, so it can't block the event loop). If `False` (the default), each predictor loads lazily on its own first use — spreading the memory cost of importing numpy/pandas/scipy/scikit-learn/xgboost over the first few requests instead of one big spike at boot.
- **`AI_MODEL_LIGHT_MODE`** (default `True` in production): immediately after loading, prunes the bundle down to just the winning model, dropping the second candidate before it's ever cached — since production predictors only ever read the winner, keeping both loaded is pure wasted memory (roughly 2x) on a memory-constrained instance.
- **Thread pinning**: every model is saved with `n_jobs=-1` from training. On load, inference is pinned to 1 thread — a single-row prediction doesn't need a thread pool sized to the host's CPU count. XGBoost needs this pinned on the native Booster's `nthread` directly, since it silently ignores the sklearn wrapper's `n_jobs` post-unpickle.
- **Graceful degradation**: if a `.pkl` is missing, or `joblib.load()` succeeds but `.predict()` fails at runtime (sklearn's version check only warns, never raises — so a model trained on a different sklearn version can "load" fine and fail later), the predictor falls back to a deterministic heuristic curve instead of a 500 error. A predict-time failure degrades one request; it never crashes the app.

### 3.3 Where predictions are consumed

- `POST /api/v1/predictions/{crowd,delay,frequency}` — single prediction, called on-demand by the frontend's AI Prediction page.
- `GET /api/v1/predictions/{crowd,delay,frequency}/metrics` — model performance (MAE etc.) for the dashboard's model-comparison view.
- `GET /api/v1/predictions/recommendations/{station_id}` and `/bulk` — turns raw predictions into human-readable scheduling recommendations.
- The **simulator** also calls into these predictors on each tick to keep live crowd/train state internally consistent with what the model would predict.

### 3.4 Chatbot (separate from the 3 predictors)

`app/services/chatbot_service.py` calls the **Gemini API** (`gemini-3.5-flash`, configurable via `CHATBOT_MODEL`) with function-calling tools defined in `chatbot_tools.py` — the model never invents numbers; it calls real DB-backed tools (station status, active alerts, busiest stations, system-wide delay summary) and answers from the actual returned data. Disabled gracefully if `GEMINI_API_KEY` is not set.

## 4. Real-time layer

- **Simulator** (`app/simulator/csv_replay_simulator.py`): replays real historical per-station ridership CSVs row-by-row on a fixed interval (`SIMULATOR_INTERVAL_SECONDS`), driving live crowd counts, check-ins/check-outs, and train movement — so the environment behaves like a live system without needing real IoT sensors.
- **Leader election** (`app/simulator/leader_election.py`): a Redis-lease based mutual-exclusion mechanism. Ensures the simulator / train tracker / retention jobs run in exactly **one** process at a time, even when `WEB_CONCURRENCY > 1` or multiple replicas are deployed behind a load balancer. Falls back to a same-host local lock if Redis is briefly unreachable, and fails safe across the transition.
- **WebSocket** (`/ws/monitor`, `app/websocket/manager.py`): broadcasts crowd updates, train positions, alerts, and notifications to connected dashboards. A Redis pub/sub relay means an event generated by *any* worker process reaches *every* connected client, regardless of which process they're attached to. Bounded connection count, LRU-capped event de-duplication, and a stale-connection reaper — no unbounded memory growth.

## 5. Authentication

Auth is fully delegated to **Supabase**. The backend never sees a password — it verifies the JWT Supabase already issued (HS256 shared secret, or JWKS-based RS256/ES256 with a rotating-key cache), then looks up or lazily creates the matching `user_profiles` row for role-based access control. A dev-only bypass exists (`AUTH_DISABLED=True`) but is only honored when `DEBUG=True` as well, so it can never accidentally disable real auth in production.

## 6. Database

PostgreSQL, 21 tables (stations, trains, schedules, crowd logs + hourly rollups, journeys/check-ins, alerts, notifications + dispatch jobs, enquiries, news, user profiles, predictions, AI model registry, metro lines/routes). Schema lives in a single Alembic baseline migration (`migrations/versions/0001_baseline_schema.py`), with `alembic upgrade head` run automatically before the app starts serving traffic.

## 7. Project layout

```
app/
  ai_engine/         3 ML predictors + shared bundle loader + saved_models/*.pkl
  api/v1/            One router file per resource
  core/              config, security (JWT), cache (Redis), rate limiting, metrics
  database/          SQLAlchemy session/engine + one-off migration/seed scripts
  enums/             All status/role/type enums
  models/            SQLAlchemy ORM models
  schemas/           Pydantic request/response schemas
  services/          Business logic per domain
  simulator/         Live-data simulator + leader election + scheduler
  websocket/         Connection manager + event relay
  main.py            App factory, lifespan (startup/shutdown), route registration
migrations/          Alembic
tests/               Pytest suite (not shipped in the Docker image)
docs/                Deep-dive design docs per subsystem
colab_training/      Offline notebooks used to train the 3 .pkl bundles
datasets/            source/ (shipped, read at runtime) + ml/ (training-only, NOT shipped)
```

## 8. Local development

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\Activate

pip install -r requirements.txt
cp .env.example .env            # fill in Supabase keys, DB/Redis URLs

python -m app.database.init_db          # create tables (runs alembic upgrade head)
python -m app.database.seed_real_data   # seed real stations/trains/schedules
python -m app.database.set_user_role <your-email> admin   # after signing up via the frontend

uvicorn app.main:app --reload
```

Or with Docker (recommended — matches production exactly):
```bash
docker compose up -d --build
curl http://localhost:8000/healthz
```

See `DEPLOYMENT.md` for Render / AWS EC2 deployment steps.
