"""
Stage 3 — Train candidate models, evaluate them, and export the winner.

Split strategy: chronological, not random. Consecutive 5-minute samples from the same
zone are near-duplicates, so a random split would leak almost every test row's answer
into training and report an inflated score. Training on the earlier portion and testing
on the later one is the honest measurement, and matches how the model is actually used
— fitted once offline, then run on data it has never seen.

Selection: lowest test MAE wins, with an efficiency tie-break. MAE is in the same
units as the target (panel output fraction, 0-1), so it reads directly as "how far off
the brightness command is". Where several models are statistically indistinguishable —
within MAE_TIE_TOLERANCE relative MAE of the leader — the smallest serialised model
wins, because this artifact is loaded into a live server process that also holds a
Whisper model in memory, and a fraction of a percent of accuracy is not worth hundreds
of megabytes of resident set.

The winner is serialised to laptop-server/model/ together with the exact feature order
and the metadata the server needs to load and sanity-check it at startup.
"""

import json
import tempfile
import time
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.dummy import DummyRegressor
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.neural_network import MLPRegressor
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from config import MODEL_DIR, OUT_DIR, RANDOM_SEED
from features import FEATURES, TARGET

TEST_FRACTION = 0.20

# Models within this relative MAE distance of the leader are treated as tied on
# accuracy, and decided on serialised size instead. 2% of an MAE of ~0.13 is ~0.003
# panel-output fraction, i.e. a third of one percent of brightness — invisible.
MAE_TIE_TOLERANCE = 0.02


def chronological_split(df):
    """Split on time: earliest 80% trains, latest 20% tests."""
    df = df.sort_values("timestamp", ignore_index=True)
    cut = int(len(df) * (1.0 - TEST_FRACTION))
    boundary = df.timestamp.iloc[cut]
    train, test = df.iloc[:cut], df.iloc[cut:]
    print(f"Chronological split at {boundary}")
    print(f"  train {len(train):>7,} rows  "
          f"({train.timestamp.min()} .. {train.timestamp.max()})")
    print(f"  test  {len(test):>7,} rows  "
          f"({test.timestamp.min()} .. {test.timestamp.max()})")
    return train, test


def candidates():
    """The model zoo. Each entry is (name, estimator, note-for-the-writeup)."""
    models = [
        ("Mean baseline",
         DummyRegressor(strategy="mean"),
         "Predicts the training mean. Floor for what counts as learning anything."),
        ("Ridge regression",
         make_pipeline(StandardScaler(), Ridge(alpha=1.0)),
         "Linear baseline on standardised features."),
        ("Random Forest",
         RandomForestRegressor(n_estimators=120, max_depth=18, min_samples_leaf=5,
                               n_jobs=-1, random_state=RANDOM_SEED),
         "Bagged trees, captures interactions without tuning."),
        ("Neural net (MLP)",
         make_pipeline(StandardScaler(),
                       MLPRegressor(hidden_layer_sizes=(64, 32), max_iter=60,
                                    early_stopping=True, random_state=RANDOM_SEED)),
         "Small dense net, two hidden layers."),
    ]

    # Gradient boosting: prefer LightGBM, fall back to XGBoost, then to sklearn's own
    # implementation, so the pipeline runs wherever it lands.
    try:
        from lightgbm import LGBMRegressor
        models.append(
            ("LightGBM",
             LGBMRegressor(n_estimators=600, learning_rate=0.05, num_leaves=63,
                           subsample=0.8, colsample_bytree=0.8,
                           random_state=RANDOM_SEED, n_jobs=-1, verbose=-1),
             "Histogram gradient boosting."))
    except ImportError:
        pass

    try:
        from xgboost import XGBRegressor
        models.append(
            ("XGBoost",
             XGBRegressor(n_estimators=600, learning_rate=0.05, max_depth=8,
                          subsample=0.8, colsample_bytree=0.8,
                          random_state=RANDOM_SEED, n_jobs=-1, tree_method="hist"),
             "Gradient boosted trees."))
    except ImportError:
        pass

    return models


def evaluate(y_true, y_pred):
    # Predictions are a panel output fraction, so clip to the physically valid range
    # before scoring — a model cannot command -3% or 140% brightness.
    y_pred = np.clip(y_pred, 0.0, 1.0)
    return {
        "mae": float(mean_absolute_error(y_true, y_pred)),
        "rmse": float(np.sqrt(mean_squared_error(y_true, y_pred))),
        "r2": float(r2_score(y_true, y_pred)),
    }


def main():
    df = pd.read_parquet(OUT_DIR / "dataset.parquet")
    print(f"Dataset: {len(df):,} rows, {len(FEATURES)} features\n")

    train, test = chronological_split(df)
    x_train, y_train = train[FEATURES], train[TARGET]
    x_test, y_test = test[FEATURES], test[TARGET]

    results = []
    fitted = {}
    print("\nTraining candidates:")
    for name, model, note in candidates():
        started = time.time()
        model.fit(x_train, y_train)
        elapsed = time.time() - started
        metrics = evaluate(y_test, model.predict(x_test))
        metrics.update(model=name, note=note, fit_seconds=round(elapsed, 1))
        results.append(metrics)
        fitted[name] = model
        print(f"  {name:<18} MAE {metrics['mae']:.4f}  "
              f"RMSE {metrics['rmse']:.4f}  R2 {metrics['r2']:>7.4f}  "
              f"({elapsed:.1f}s)")

    # The system this replaces: a fixed 100%-brightness panel. Scoring it on the same
    # test set gives the honest "what does the ML actually buy us" comparison.
    always_on = evaluate(y_test, np.ones(len(y_test)))
    always_on.update(model="Always 100% (incumbent)", note="The naive system being "
                     "replaced — panel pinned at full output.", fit_seconds=0.0)
    results.append(always_on)
    print(f"  {'Always 100%':<18} MAE {always_on['mae']:.4f}  "
          f"RMSE {always_on['rmse']:.4f}  R2 {always_on['r2']:>7.4f}  (incumbent)")

    table = pd.DataFrame(results)[
        ["model", "mae", "rmse", "r2", "fit_seconds", "note"]
    ].sort_values("mae", ignore_index=True)
    table.to_csv(OUT_DIR / "model_comparison.csv", index=False)

    # --- SELECT WINNER ------------------------------------------------------
    # Only genuine candidates compete; the incumbent is a reference line, not a model.
    ranked = table[table.model.isin(fitted)].reset_index(drop=True)
    leader_mae = ranked.mae.iloc[0]
    tied = ranked[ranked.mae <= leader_mae * (1.0 + MAE_TIE_TOLERANCE)].copy()

    # Measure each tied model's serialised footprint, since that is the cost the
    # server actually pays for it.
    with tempfile.TemporaryDirectory() as tmp:
        tied["size_mb"] = [
            round(joblib.dump(fitted[n], Path(tmp) / "m.joblib")
                  and (Path(tmp) / "m.joblib").stat().st_size / 1e6, 1)
            for n in tied.model
        ]

    if len(tied) > 1:
        print(f"\nTied within {MAE_TIE_TOLERANCE:.0%} relative MAE of the leader "
              f"({leader_mae:.4f}) — deciding on serialised size:")
        for _, row in tied.iterrows():
            print(f"  {row.model:<18} MAE {row.mae:.4f}  {row.size_mb:>7.1f} MB")

    best_row = tied.sort_values("size_mb", ignore_index=True).iloc[0]
    best_name = best_row.model
    best_model = fitted[best_name]
    best_metrics = best_row

    print(f"\nWinner: {best_name}  "
          f"(MAE {best_metrics.mae:.4f}, RMSE {best_metrics.rmse:.4f}, "
          f"R2 {best_metrics.r2:.4f}, {best_metrics.size_mb} MB)")

    # --- EXPORT -------------------------------------------------------------
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(best_model, MODEL_DIR / "brightness_model.joblib")

    metadata = {
        "model": best_name,
        "features": FEATURES,          # order matters: the server builds its vector from this
        "target": TARGET,
        "target_range": [0.0, 1.0],
        "target_description": "Panel output fraction, 0.0-1.0.",
        "metrics": {"mae": best_metrics.mae, "rmse": best_metrics.rmse,
                    "r2": best_metrics.r2},
        "model_size_mb": float(best_metrics.size_mb),
        "selection_rule": (f"lowest test MAE, with models within "
                           f"{MAE_TIE_TOLERANCE:.0%} relative MAE decided on "
                           f"serialised size"),
        "incumbent_always_100pct": {k: always_on[k] for k in ("mae", "rmse", "r2")},
        "trained_rows": int(len(train)),
        "test_rows": int(len(test)),
        "split": "chronological, last 20% held out",
        "sklearn_target_note": "Server must clip predictions to [0, 1].",
        "input_note": ("Build the input as a pandas DataFrame with exactly the "
                       "`features` columns in that order; passing a bare array "
                       "triggers a feature-name warning."),
    }
    (MODEL_DIR / "model_metadata.json").write_text(json.dumps(metadata, indent=2))

    print(f"Exported to {MODEL_DIR}/brightness_model.joblib")
    print(f"Comparison table: {OUT_DIR}/model_comparison.csv")

    print("\n" + table.drop(columns="note").to_string(index=False))


if __name__ == "__main__":
    main()
