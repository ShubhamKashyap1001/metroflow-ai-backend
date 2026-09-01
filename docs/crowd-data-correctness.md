# Crowd data correctness — what the numbers actually mean

This covers how "current occupancy", "station capacity", and "crowd
level" are computed from the real dataset (`datasets/passenger_flow.csv.gz`),
and why the obvious-looking formula (`entries + exits`) is wrong for
occupancy even though it's the right quantity elsewhere. For where the
data lives and how it's kept live/bounded, see
[crowd-live-state-and-retention.md](./crowd-live-state-and-retention.md).

## Ground truth, measured from the dataset

Before any of this was implemented, the dataset's own columns were
used to work out what they actually mean — nothing here is assumed:

| Column | What it actually is |
|---|---|
| `entries` / `exits` | **Hourly throughput** through the gates, not a point-in-time headcount. `(entries+exits)` ranges from 1 to 36,586 in a single station-hour — no station physically holds 36,586 people, so this is clearly a flow count. |
| `crowding_index` | `(entries + exits) / capacity`, clipped at 1.5 — a throughput-relative-to-capacity index, deliberately allowed to exceed 1.0 to represent real overcrowding. |
| `crowding_label` | A categorical bucketing of `crowding_index`: `Low` < 0.15, `Normal` 0.15–0.349, `Crowded` 0.35–0.599, `Critically Overcrowded` ≥ 0.6 (measured directly from the data's own label boundaries). |
| `capacity` | Not present as a column — recovered by solving `crowding_index = (entries+exits)/capacity` per station. |
| Daily net flow (`entries - exits` over a real calendar day) | Median ≈ 9 passengers, ≈1.5% of that day's ridership — stations really do empty back out to roughly where they started by day's end. |

## Occupancy: a running net-flow accumulator, not `entries + exits`

`csv_replay_simulator._current_count_from_row()` used to compute the
live "current occupancy" as `entries + exits` for the current row —
i.e. hourly throughput, not a headcount of people currently inside the
station. Fixed with a real running accumulator per station:

```python
occupancy = max(0, occupancy + entries - exits)
```

- Clamped at 0 (more exits than the accumulator currently holds is a
  counting artifact, not a real negative headcount).
- **Not** hard-capped at capacity — `crowding_index` intentionally
  goes above 1.0 to represent real overcrowding, and hard-capping
  occupancy would erase that signal before it reaches the crowd-level
  logic.
- **Reset at real day boundaries** — grounded in the measured fact
  above that stations return close to empty overnight, so starting a
  fresh accumulator each day reflects that instead of letting drift
  accumulate forever.
- Crowd *level* continues to prefer the dataset's own `crowding_label`
  column when present — only the occupancy *number* was wrong; level
  was already correctly decoupled from it.

Verified against station `STN-AMD-RL-01`'s real first calendar day: the
output never equals `entries+exits` for any of the 18 real rows,
matches a hand-verified sequence computed independently
(`[0, 6, 6, 9, 0, 0, 0, 0, 0, 2, 7, 15, 18, 22, 0, 15, 10, 22]`), never
goes negative even on rows with more exits than entries, and correctly
resets to 0 at the next real day boundary rather than carrying the
previous day's leftover forward.

## Capacity: derived per station, not a flat placeholder

Every one of the 324 real stations used to be seeded with a hardcoded
`capacity=5000`, regardless of the station's real size or throughput —
a placeholder that was never replaced. Since `crowding_index =
(entries+exits)/capacity` holds for (almost) every row, solving for
capacity per row and taking the **per-station median** (median, not
mean, so a handful of clipped/extreme rows near the 1.5 ceiling can't
skew it) recovers a real, station-specific capacity.
`_derive_station_capacities()` (`app/database/seed_real_data.py`) does
this at seed time; rows with `crowding_index == 0` are excluded first
(would divide by zero); `DEFAULT_CAPACITY = 2400` is kept as a
last-resort fallback for a station with zero valid flow rows (none
exist in the real dataset).

Measured result across all 324 real stations: mean capacity 2,438.1,
range 2,000–3,050, coefficient of variation 5.88% — a real,
station-specific spread instead of one flat number.

## Seeding: initial data used the same throughput-as-occupancy mistake

Two related gaps in `seed_real_data.py`, fixed together:

1. The seed script's crowd-seeding block averaged `entries.clip(0) +
   exits.clip(0)` across the **entire 4-month dataset** per station and
   inserted that single averaged throughput number as the seed
   `CrowdLog.current_count` — the same throughput-as-occupancy mistake
   as the live simulator, just in the seed script.
2. `station_crowd_state` (the live table) was never seeded at all — it
   stayed empty until the simulator's first tick after startup.

`_build_seed_crowd_rows()` walks each station's own real first
calendar day of rows through the same net-flow accumulator logic as
the fixed simulator, producing one real, correctly-computed `CrowdLog`
row per real hour of that day (genuine history, not a fabricated
average), and uses the last hour's occupancy/level as that station's
initial `station_crowd_state` row. Measured result: 5,832 historical
rows seeded across all stations (vs. 324 fabricated averages before),
324 live-state rows seeded immediately (vs. 0 before), zero negative
values.

## Input validation: negative occupancy

`CrowdLogCreate.current_count` (the schema behind manual/sensor `POST
/crowd/` readings) had no lower bound — a negative value (typo, buggy
upstream sensor) could reach `crowd_logs` and `station_crowd_state`
directly. Fixed with `current_count: int = Field(ge=0)` — rejected at
the API boundary as a 422 instead of corrupting downstream data.

## AI alert threshold: capacity-relative, not a flat constant

`prediction_service._build_recommendations()` used to fire "High crowd
expected" whenever `predicted_count > 800`, for every station
regardless of scale — a small station whose real throughput rarely
exceeds ~200 would never trigger it; a large interchange whose normal
throughput comfortably exceeds 800 would trigger it constantly
(alert fatigue), regardless of whether either station was actually
unusually crowded relative to itself.

`predicted_count` is the model's predicted hourly throughput (the same
quantity the crowd model was trained on: `passenger_count = entries +
exits`). Fixed by comparing it against the station's own capacity:
`predicted_count / station.capacity > HIGH_CROWD_THROUGHPUT_RATIO`
where `HIGH_CROWD_THROUGHPUT_RATIO = 0.35` — the same "Crowded" cutoff
measured directly off the dataset's own `crowding_index` (see the
ground-truth table above and `CrowdLevel`'s thresholds below). This
means "flag this as high crowd exactly when the dataset itself would
call this station's real historical throughput 'Crowded' or worse" —
not an arbitrary new number. Both `smart_recommendations()` and
`smart_recommendations_bulk()` now fetch and pass the station's real
capacity (the bulk path does one batched `Station.id, Station.capacity`
lookup, avoiding N+1). `_build_recommendations()`'s `capacity`
parameter falls back to `DEFAULT_CAPACITY_FALLBACK = 2400` (the
network-wide median) if ever called without one.

Is the crowd model's `passenger_count = entries + exits` training
target itself wrong, then? No retraining needed: the dataset's own
`crowding_index` — the ground-truth crowd signal the whole system is
calibrated against — is *itself* defined as `(entries+exits)/capacity`.
The dataset treats throughput-relative-to-capacity as the definition
of crowding, not net occupancy. Training the model to predict
throughput and comparing that prediction to capacity using the
dataset's own thresholds (this fix) is internally consistent with how
the dataset defines crowding — it's a genuinely different quantity
from the occupancy accumulator above (which answers "how many people
are in the station right now"), and both are now used for what they
each actually measure: occupancy accumulator → live dashboard
`current_count`; AI-predicted throughput ÷ capacity → crowd-level
alerts/recommendations.

## Crowd-level thresholds

`CrowdLevel.from_ratio()` (`app/enums/crowd_level.py`) is the fallback
used whenever a real `crowding_label` isn't available (manual
readings, check-in/check-out deltas). Its cutoffs used to be `0.4 /
0.7 / 0.9`, which didn't match the real data's own label boundaries at
all. Measured directly from the dataset
(`df.groupby("crowding_label")["crowding_index"].agg(["min","max"])`):
Low `[0.001, 0.149]`, Normal `[0.150, 0.349]`, Crowded `[0.350, 0.599]`,
Critically Overcrowded `[0.600, 1.500]`. Updated to `0.15 / 0.35 / 0.6`
to match exactly, with the constants named
(`LOW_MAX_RATIO`/`MODERATE_MAX_RATIO`/`HIGH_MAX_RATIO`) so the AI
threshold above references the same single source of truth for the
"Crowded" cutoff instead of a second hardcoded copy.

## Realtime path — unaffected

None of this changes the transport: the frontend already consumes the
`crowd_update` WebSocket event from both the simulator tick and
manual/check-in writes (see
[crowd-live-state-and-retention.md](./crowd-live-state-and-retention.md)
and [realtime-websocket-system.md](./realtime-websocket-system.md)).
This work only changed the *values* being broadcast — real occupancy
instead of throughput, real per-station capacity — so no frontend
changes were needed.
