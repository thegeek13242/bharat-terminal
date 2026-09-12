"""Circular block bootstrap for the mean of a dependent series.

Blocks preserve the local dependence that overlapping event windows create.
Seeded with FACTORY_SEED everywhere, and the seed is recorded on the run row,
so a confidence interval is reproducible byte for byte.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from bharat_terminal.factory import FACTORY_SEED


@dataclass(frozen=True)
class BootstrapCI:
    mean: float
    lo: float
    hi: float
    n_resamples: int
    block_length: int
    seed: int
    share_above_zero: float


def default_block_length(n: int) -> int:
    """n**(1/3), the usual rule of thumb, clamped to [1, n]."""
    if n <= 1:
        return max(1, n)
    return max(1, min(n, int(round(n ** (1.0 / 3.0)))))


def block_bootstrap_ci(
    series: Sequence[float],
    n_resamples: int = 10_000,
    alpha: float = 0.05,
    block_length: int | None = None,
    seed: int = FACTORY_SEED,
) -> BootstrapCI:
    x = np.asarray(list(series), dtype=float)
    n = x.size
    if n == 0:
        return BootstrapCI(0.0, 0.0, 0.0, 0, 0, seed, 0.0)
    if n == 1:
        v = float(x[0])
        return BootstrapCI(v, v, v, 0, 1, seed, 1.0 if v > 0 else 0.0)

    L = int(block_length or default_block_length(n))
    L = max(1, min(L, n))
    n_blocks = int(math.ceil(n / L))

    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n, size=(n_resamples, n_blocks))
    offsets = np.arange(L)
    # Circular indexing keeps every observation equally likely.
    idx = (starts[:, :, None] + offsets[None, None, :]) % n
    samples = x[idx.reshape(n_resamples, -1)[:, :n]]
    means = samples.mean(axis=1)

    lo, hi = np.quantile(means, [alpha / 2.0, 1.0 - alpha / 2.0])
    return BootstrapCI(
        mean=float(x.mean()),
        lo=float(lo),
        hi=float(hi),
        n_resamples=n_resamples,
        block_length=L,
        seed=seed,
        share_above_zero=float((means > 0).mean()),
    )
