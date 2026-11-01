import pandas as pd

def drop_invalid_rows(df: pd.DataFrame) -> pd.DataFrame:
    required = [c for c in ["station_id", "hour", "passenger_count"] if c in df.columns]
    return df.dropna(subset=required)

def clip_outliers(df: pd.DataFrame, column: str, upper_quantile: float = 0.99) -> pd.DataFrame:
    if column not in df.columns:
        return df
    cap = df[column].quantile(upper_quantile)
    df[column] = df[column].clip(upper=cap)
    return df
