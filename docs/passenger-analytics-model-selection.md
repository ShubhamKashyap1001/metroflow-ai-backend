# Passenger Analytics — crowd model selection

Covers why the dashboard's "Passenger Analytics" chart
(`getAggregateTrafficPattern`, backed by
`GET /api/v1/predictions/traffic-pattern-aggregate/all`) showed a real,
believable 24h demand curve for Delhi and Kolkata but a flat near-zero
line for every other seeded city — and for the default "All Cities"
view.

## Root cause

`colab_training/train_crowd_model.py` trains two candidate regressors
(RandomForest, XGBoost) and used to pick the "winner" by raw held-out
**MAE**. `passenger_count` (the training target) spans a huge range
across cities — Delhi averages ~5,700 passengers/hour at a station,
Pune/Bhopal average ~20–40. A model that fits Delhi's large numbers
well can "win" on raw MAE even while being systematically wrong
(including predicting negative values, which `crowd_predictor.py`
already clamps to 0) for every smaller-scale city, because those
cities' errors are individually tiny in *absolute* terms and barely
move the total MAE. That's exactly what had happened: the shipped
`crowd_model.pkl`'s XGBoost "winner" predicted near-zero for stations
outside Delhi/Kolkata's scale, and `all_stations_traffic_pattern()`
faithfully summed those near-zero per-station predictions into a
near-zero total for every other city.

This was never a data-coverage problem —
`datasets/passenger_flow.csv.gz` already has real, non-trivial rows
for all 12 cities, and station-id alignment between the training table
and the seeded DB was verified correct (same source CSV, same
dedup/dropna/iteration order). It was purely a model *selection* bug.

## Fix

1. `train_crowd_model.py` now selects the winning candidate by
   **MAPE** (mean absolute percentage error) instead of raw MAE, so
   every city has to be predicted reasonably well in *relative* terms
   to win — a model can no longer coast to "best" purely by nailing
   Delhi's big numbers.
2. Re-trained `crowd_model.pkl` on the current, full 12-city dataset.
   Verified the RandomForest candidate now predicts sane, distinct,
   non-zero passenger counts for every one of the 12 seeded cities (not
   just Delhi/Kolkata) at typical peak hours.
3. Frontend: the "Passenger Analytics" subtitle was hardcoded to
   always read "All Stations" even when a specific city was selected —
   `PassengerChart.tsx` now reflects the actual filter.

## Note for whoever next runs Colab training

The shipped fix ships with only the RandomForest candidate — the
environment it was built in had no way to install `xgboost` to re-run
the two-way comparison. Re-running `train_crowd_model.py` in Colab
(where `xgboost` is available) will fairly compare both candidates
again under the new MAPE-based selection, and may bring XGBoost back
if it genuinely predicts well across all cities' scales under that
metric.
