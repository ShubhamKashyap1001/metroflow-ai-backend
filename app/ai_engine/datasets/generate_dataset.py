"""Generates a synthetic ridership/demand dataset shaped like the real
"Passenger Datasets" and "Transportation Datasets" described in the
platform spec (smart-card, entry/exit, footfall, ridership, GPS/status,
delay logs, peak-hour traffic) so the Milestone 2 models have
something concrete to train on before real data feeds are connected.

Run directly:  python -m app.ai_engine.datasets.generate_dataset
"""
import os
import random

import numpy as np
import pandas as pd

random.seed(42)
np.random.seed(42)

OUTPUT_DIR = os.path.dirname(__file__)
NUM_STATIONS = 20

def _base_demand(hour: int, is_weekend: int) -> float:
    """Rough double-hump commuter curve, dampened on weekends."""
    morning_peak = np.exp(-((hour - 9) ** 2) / 4) * 900
    evening_peak = np.exp(-((hour - 18.5) ** 2) / 5) * 950
    base = 150 + morning_peak + evening_peak
    if is_weekend:
        base *= 0.55
    return base

def generate_ridership_dataset(days: int = 60) -> pd.DataFrame:
    rows = []
    for station_id in range(1, NUM_STATIONS + 1):
                                                                                 
        station_multiplier = np.random.uniform(0.6, 1.8)
        for day in range(days):
            day_of_week = day % 7
            is_weekend = 1 if day_of_week in (5, 6) else 0
            for hour in range(24):
                demand = _base_demand(hour, is_weekend) * station_multiplier
                noise = np.random.normal(0, demand * 0.08)
                passenger_count = max(0, int(demand + noise))

                congestion_factor = passenger_count / 1000
                delay_minutes = max(0, np.random.normal(congestion_factor * 4, 1.5))

                rows.append({
                    "station_id": station_id,
                    "day": day,
                    "day_of_week": day_of_week,
                    "is_weekend": is_weekend,
                    "hour": hour,
                    "is_peak_hour": 1 if (8 <= hour <= 11 or 17 <= hour <= 20) else 0,
                    "passenger_count": passenger_count,
                    "delay_minutes": round(float(delay_minutes), 2),
                })

    return pd.DataFrame(rows)

def save_dataset(path: str = None) -> str:
    df = generate_ridership_dataset()
    path = path or os.path.join(OUTPUT_DIR, "ridership_data.csv")
    df.to_csv(path, index=False)
    return path

if __name__ == "__main__":
    out_path = save_dataset()
    print(f"Generated synthetic ridership dataset -> {out_path}")
