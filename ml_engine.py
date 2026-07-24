"""Lightweight ML layer for Polymarket signals.

Implements a small logistic-regression trainer using only Python's standard
library. The model stays in shadow mode and never blocks notifications.
"""

from __future__ import annotations

import json
import math
import os
import random
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from feature_engine import FEATURE_NAMES, vector_from_features

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
MODEL_PATH = os.path.join(DATA_DIR, "polymarket_ml_model.json")


def _sigmoid(value: float) -> float:
    value = max(-35.0, min(35.0, value))
    return 1.0 / (1.0 + math.exp(-value))


def _dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def load_model() -> Optional[dict[str, Any]]:
    try:
        with open(MODEL_PATH, "r", encoding="utf-8") as file:
            model = json.load(file)
    except (OSError, ValueError, TypeError):
        return None

    if model.get("feature_names") != FEATURE_NAMES:
        return None
    if len(model.get("weights", [])) != len(FEATURE_NAMES):
        return None
    return model


def predict_probability(features: dict[str, float]) -> Optional[float]:
    model = load_model()
    if model is None:
        return None

    vector = vector_from_features(features)
    means = model["means"]
    scales = model["scales"]
    standardized = [
        (value - means[index]) / scales[index]
        for index, value in enumerate(vector)
    ]
    return _sigmoid(model["bias"] + _dot(model["weights"], standardized))


def train_model(
    samples: Iterable[tuple[dict[str, float], int]],
    *,
    min_samples: int = 200,
    epochs: int = 350,
    learning_rate: float = 0.04,
    l2: float = 0.002,
) -> dict[str, Any]:
    rows = [(vector_from_features(features), int(label)) for features, label in samples]
    if len(rows) < min_samples:
        return {"trained": False, "reason": "not_enough_samples", "samples": len(rows)}

    positives = sum(label for _, label in rows)
    negatives = len(rows) - positives
    if positives < 20 or negatives < 20:
        return {
            "trained": False,
            "reason": "class_imbalance",
            "samples": len(rows),
            "positives": positives,
            "negatives": negatives,
        }

    dimension = len(FEATURE_NAMES)
    means = [sum(row[0][i] for row in rows) / len(rows) for i in range(dimension)]
    scales: list[float] = []
    for i in range(dimension):
        variance = sum((row[0][i] - means[i]) ** 2 for row in rows) / len(rows)
        scales.append(max(math.sqrt(variance), 1e-6))

    standardized = [
        ([ (x[i] - means[i]) / scales[i] for i in range(dimension) ], y)
        for x, y in rows
    ]

    rng = random.Random(42)
    rng.shuffle(standardized)
    split = max(int(len(standardized) * 0.8), 1)
    train_rows = standardized[:split]
    test_rows = standardized[split:] or standardized[-min(20, len(standardized)):]

    weights = [0.0] * dimension
    bias = 0.0

    for _ in range(epochs):
        gradient = [0.0] * dimension
        bias_gradient = 0.0
        for vector, label in train_rows:
            prediction = _sigmoid(bias + _dot(weights, vector))
            error = prediction - label
            bias_gradient += error
            for i in range(dimension):
                gradient[i] += error * vector[i]

        count = float(len(train_rows))
        bias -= learning_rate * bias_gradient / count
        for i in range(dimension):
            weights[i] -= learning_rate * (gradient[i] / count + l2 * weights[i])

    predictions = [
        1 if _sigmoid(bias + _dot(weights, vector)) >= 0.5 else 0
        for vector, _ in test_rows
    ]
    accuracy = sum(
        int(prediction == label)
        for prediction, (_, label) in zip(predictions, test_rows)
    ) / len(test_rows)

    model = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "feature_names": FEATURE_NAMES,
        "weights": weights,
        "bias": bias,
        "means": means,
        "scales": scales,
        "samples": len(rows),
        "positives": positives,
        "negatives": negatives,
        "validation_accuracy": accuracy,
        "target": "maximum YES-price gain of at least 20% within 24h",
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    temporary_path = MODEL_PATH + ".tmp"
    with open(temporary_path, "w", encoding="utf-8") as file:
        json.dump(model, file, ensure_ascii=False, indent=2)
    os.replace(temporary_path, MODEL_PATH)

    return {"trained": True, **model}
