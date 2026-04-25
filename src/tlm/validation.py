from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date

    def to_dict(self) -> dict:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


@dataclass(frozen=True)
class ValidationFold:
    index: int
    train: DateRange
    validation: DateRange
    test: DateRange

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "train": self.train.to_dict(),
            "validation": self.validation.to_dict(),
            "test": self.test.to_dict(),
        }


@dataclass(frozen=True)
class ValidationPlan:
    folds: list[ValidationFold]
    final_holdout: DateRange
    embargo_days: int

    def to_dict(self) -> dict:
        return {
            "folds": [fold.to_dict() for fold in self.folds],
            "final_holdout": self.final_holdout.to_dict(),
            "embargo_days": self.embargo_days,
            "overlapping_test_folds": has_overlapping_test_folds(self.folds),
            "non_overlap_test_fold_indexes": non_overlapping_test_fold_indexes(self.folds),
        }


def generate_rolling_folds(
    start: date,
    end: date,
    train_days: int = 730,
    validation_days: int = 182,
    test_days: int = 182,
    step_days: int = 91,
    embargo_days: int = 5,
    final_holdout_days: int = 365,
    min_folds: int = 1,
) -> ValidationPlan:
    if end <= start:
        raise ValueError("end must be after start")
    holdout_start = end - timedelta(days=final_holdout_days - 1)
    if holdout_start <= start:
        raise ValueError("date range too short for final holdout")

    folds: list[ValidationFold] = []
    cursor = start
    index = 0
    latest_test_end = holdout_start - timedelta(days=embargo_days + 1)
    while True:
        train_start = cursor
        train_end = train_start + timedelta(days=train_days - 1)
        validation_start = train_end + timedelta(days=embargo_days + 1)
        validation_end = validation_start + timedelta(days=validation_days - 1)
        test_start = validation_end + timedelta(days=embargo_days + 1)
        test_end = test_start + timedelta(days=test_days - 1)
        if test_end > latest_test_end:
            break
        folds.append(
            ValidationFold(
                index=index,
                train=DateRange(train_start, train_end),
                validation=DateRange(validation_start, validation_end),
                test=DateRange(test_start, test_end),
            )
        )
        index += 1
        cursor += timedelta(days=step_days)

    if len(folds) < min_folds:
        raise ValueError(f"Only generated {len(folds)} folds; min_folds={min_folds}")
    return ValidationPlan(
        folds=folds,
        final_holdout=DateRange(holdout_start, end),
        embargo_days=embargo_days,
    )


def has_overlapping_test_folds(folds: list[ValidationFold]) -> bool:
    ordered = sorted(folds, key=lambda fold: fold.test.start)
    previous_end: date | None = None
    for fold in ordered:
        if previous_end is not None and fold.test.start <= previous_end:
            return True
        previous_end = max(previous_end, fold.test.end) if previous_end else fold.test.end
    return False


def non_overlapping_test_fold_indexes(folds: list[ValidationFold]) -> list[int]:
    selected: list[int] = []
    last_end: date | None = None
    for fold in sorted(folds, key=lambda item: (item.test.start, item.index)):
        if last_end is None or fold.test.start > last_end:
            selected.append(fold.index)
            last_end = fold.test.end
    return selected
