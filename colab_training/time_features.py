"""Feature engineering helpers shared by the training scripts and the
live predictors, so features are computed identically at train and
inference time.
"""
import pandas as pd

def add_time_features(df: pd.DataFrame, timestamp_col: str = "timestamp") -> pd.DataFrame:
    if timestamp_col in df.columns:
        ts = pd.to_datetime(df[timestamp_col])
        df["hour"] = ts.dt.hour
        df["day_of_week"] = ts.dt.dayofweek
    df["is_weekend"] = df["day_of_week"].isin([5, 6]).astype(int)
    df["is_peak_hour"] = df["hour"].apply(
        lambda h: 1 if (8 <= h <= 11 or 17 <= h <= 20) else 0
    )
    return df
