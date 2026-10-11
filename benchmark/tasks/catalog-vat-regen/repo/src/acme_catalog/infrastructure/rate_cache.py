"""An instrumented cache for hot lookups. In production it reports hit rates to the metrics pipeline."""
from __future__ import annotations

import functools

_stats = {"hits": 0, "misses": 0}


def memoised(function):
    cached = functools.lru_cache(maxsize=None)(function)

    @functools.wraps(function)
    def wrapper(*args):
        hits_before = cached.cache_info().hits
        result = cached(*args)
        _stats["hits" if cached.cache_info().hits > hits_before else "misses"] += 1
        return result

    wrapper.cache_clear = cached.cache_clear
    return wrapper


def stats():
    return dict(_stats)
