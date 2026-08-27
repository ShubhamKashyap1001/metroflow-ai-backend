# Model training - Colab only, now on REAL data

Training the AI models is **not** part of running this backend, and this
folder is **not** imported by `app/` in any way. `uvicorn app.main:app`
never touches anything in here, so it never costs you local CPU just to run
the API.

All 4 models now train on your **real datasets** (`../datasets/*.csv.gz`, gzipped to stay under GitHub's 100MB file limit)
instead of synthetic data:

| Model | Trained from |
|---|---|
| `crowd_model.pkl` | real `passenger_flow.csv.gz` (via `_real_dataset_builder.py`) |
| `delay_model.pkl` | real `train_operations.csv.gz` (via `_real_dataset_builder.py`) |
| `frequency_model.pkl` | derived from the same real crowd table |
| `maintenance_model.pkl` | real `predictive_maintenance.csv.gz` directly |

## Why this folder is separate

- The FastAPI app package only contains lightweight **inference** code
  (`app/ai_engine/prediction/*.py`), which just loads an already-trained
  `.pkl` file and predicts. That's fast and cheap on any machine.
- All the actually CPU-heavy work (`RandomForestRegressor.fit(...)`) happens
  here, either in **Google Colab** or, since this repo's dataset isn't huge
  (~175k passenger_flow rows, ~58k maintenance rows), locally in a few
  seconds if you'd rather skip Colab.

## Workflow (Colab)

1. Upload this whole `colab_training/` folder AND the `../datasets/` folder
   to your Colab session (or mount Google Drive with both).
2. Open `train_metroflow_models_colab.ipynb` and run all cells. It installs
   the exact package versions from `requirements.txt`, builds the real
   training tables, trains all 4 models, sanity-checks each one, and
   downloads `metroflow_models_only.zip`.
3. Unzip it and copy the four files into `app/ai_engine/saved_models/` in
   this repo, overwriting the old ones:
   - `crowd_model.pkl`
   - `delay_model.pkl`
   - `frequency_model.pkl`
   - `maintenance_model.pkl`
4. Restart `uvicorn app.main:app --reload`. Done - no training ever ran on
   your machine.

## Running locally instead of Colab (optional)

Each script in this folder still works standalone, no internet needed:

```bash
cd colab_training
python train_crowd_model.py
python train_delay_model.py
python train_frequency_model.py
python train_maintenance_model.py
```

Each writes its model into `colab_training/output/` (not
`app/ai_engine/`), so it still can't be picked up by the running app until
you manually copy the resulting `.pkl` into `app/ai_engine/saved_models/` -
same "export -> copy in" habit either way, whether trained in Colab or here.

## Data cleaning applied

`_real_dataset_builder.py` builds the crowd/delay/frequency training table
from the raw CSVs:

- Station matching: `(city, station_name)` normalized (trimmed, lowercased)
  and mapped to the same deterministic `station_id` (sort by city -> line ->
  name, then 1..N) that `app/database/seed_real_data.py` uses to seed
  Postgres - so a `station_id` in a prediction always means the same real
  station in the database.
- `entries`/`exits` clipped at 0, summed to `passenger_count`.
- `delay_arrival_min` clipped at 0 (a negative delay isn't meaningful),
  missing values treated as 0 (no delay).
- Rows grouped by `(station_id, hour, day_of_week)` and averaged, since a
  RandomForest needs one row per feature combination, not one row per
  individual trip/entry log.
- Passenger-flow data currently only covers 6 of the 12 cities in
  `stations.csv.gz` - the other 6 cities' stations exist in the DB (from
  `seed_real_data.py`) but the crowd/delay/frequency models won't have real
  training signal for them until a passenger-flow export covering those
  cities is added. `predictive_maintenance.csv.gz` and `train_operations.csv.gz`,
  by contrast, already cover all 12 cities.

`train_maintenance_model.py` uses `predictive_maintenance.csv.gz` directly (no
join needed) - it's already one row per train reading with no station
matching required.

## Files in this folder

| File | What it is |
|---|---|
| `train_metroflow_models_colab.ipynb` | The notebook - run this in Colab |
| `train_crowd_model.py` / `train_delay_model.py` / `train_frequency_model.py` / `train_maintenance_model.py` | Standalone training scripts, one per model |
| `_real_dataset_builder.py` | Builds the crowd/delay/frequency training table from `../datasets/passenger_flow.csv.gz` + `train_operations.csv.gz` + `stations.csv.gz` |
| `time_features.py` | Feature-engineering helper (unused by the running app; kept for reference/future use) |
| `clean_ridership.py` | Placeholder real-data preprocessing helper (unused by the running app; kept for reference/future use) |
