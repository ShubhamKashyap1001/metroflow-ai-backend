"""Loads the ai_engine synthetic (or, later, real) ridership dataset
for use by the simulator and any ad-hoc analysis/notebooks."""
import os

import pandas as pd

from app.ai_engine.datasets.generate_dataset import save_dataset

DATASET_PATH = os.path.join(
    os.path.dirname(__file__), "..", "ai_engine", "datasets", "ridership_data.csv"
)

def load_ridership_data() -> pd.DataFrame:
    if not os.path.exists(DATASET_PATH):
        save_dataset(DATASET_PATH)
    return pd.read_csv(DATASET_PATH)

def sample_for_hour(df: pd.DataFrame, station_id: int, hour: int) -> pd.DataFrame:
    return df[(df["station_id"] == station_id) & (df["hour"] == hour)]
