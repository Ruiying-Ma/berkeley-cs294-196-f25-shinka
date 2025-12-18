# EVOLVE-BLOCK-START
"""ARC + TinyLFU with frequency-aware tail sampling.

Public API:
- evict(cache_snapshot, obj) -> key
- update_after_hit(cache_snapshot, obj)
- update_after_insert(cache_snapshot, obj)
- update_after_evict(cache_snapshot, obj, evicted_obj)
"""

from collections import OrderedDict


class _CmSketch:
    """
    Count-Min Sketch with conservative aging (TinyLFU).
    - d hash functions, width w (power-of-two).
    - Periodic right-shift halves counters to forget stale history.
    """
    __slots__ = ("d", "w", "tables", "mask", "ops", "age_period", "seeds")

    def __init__(self, width_power=12, d=3):
        self.d = int(max(1, d))
        w = 1 << int(max(8, width_power))  # min 256
        self.w = w
        self.mask = w - 1
        self.tables = [[0] * w for _ in range(self.d)]
        self.ops = 0
        self.age_period = max(1024, w)
        self.seeds = (0x9e3779b1, 0x85ebca77, 0xc2b2ae3d, 0x27d4eb2f)

    def _hash(self, key_hash: int, i: int) -> int:
        h = key_hash ^ self.seeds[i % len(self.seeds)]
        h ^= (h >> 33) & 0xFFFFFFFFFFFFFFFF
        h *= 0xff51afd7ed558ccd
        h &= 0xFFFFFFFFFFFFFFFF
        h ^= (h >> 33)
        h *= 0xc4ceb9fe1a85ec53
        h &= 0xFFFFFFFFFFFFFFFF
        h ^= (h >> 33)
        return h & self.mask

    def _maybe_age(self):
        self.ops += 1
        if self.ops % self.age_period == 0:
            for t in self.tables:
                for i in range(self.w):
                    t[i] >>= 1

    def increment(self, key: str, amount: int = 1):
        h = hash(key)
        for i in range(self.d):
            idx = self._hash(h, i)
            v = self.tables[i][idx] + amount
            if v > 255:
                v = 255
            self.tables[i][idx] = v
        self._maybe_age()

    def estimate(self, key: str) -> int:
        h = hash(key)
        est = 1 << 30
        for i in range(self.d):
            idx = self._hash(h, i)
            v = self.tables[i][idx]
            if v < est:
                est = v
        return est


# Global ARC + TinyLFU state
T1 = OrderedDict()  # probationary (recency)
T2 = OrderedDict()  # protected (frequency)
B1 = OrderedDict()  # ghost of T1
B2 = OrderedDict()  # ghost of T2
P = 0               # ARC target for |T1|

# TinyLFU sketch
SKETCH = _CmSketch(width_power=12, d=3)

# Bookkeeping
_LAST_ACCESS = {}           # key -> last access_count
_LAST_SEEN_ACCESS = -1
_SAMPLE_K = 6               # sample count
_TAIL_MULT = 4              # sample from 4k LRU tail
_MISS_STREAK = 0            # for simple scan awareness


def _reset_if_new_run(cache_snapshot):
    """Reset metadata when a new trace/cache run starts."""
    global T1, T2, B1, B2, P, SKETCH, _LAST_ACCESS, _LAST_SEEN_ACCESS, _SAMPLE_K, _MISS_STREAK
    if cache_snapshot.access_count <= 1 or _LAST_SEEN_ACCESS > cache_snapshot.access_count:
        T1.clear(); T2.clear(); B1.clear(); B2.clear()
        P = 0
        _LAST_ACCESS.clear()
        _MISS_STREAK = 0
        # Reinit sketch
        SKETCH = _CmSketch(width_power=12, d=3)
    # capacity-aware tunables
    cap = max(int(cache_snapshot.capacity), 1)
    _SAMPLE_K = max(4, min(12, (cap // 8) or 4))
    try:
        SKETCH.age_period = max(512, min(16384, cap * 8))
    except Exception:
        pass
    _LAST_SEEN_ACCESS = cache_snapshot.access_count


def _prune_metadata(cache_snapshot):
    """Remove phantom entries not in cache from T1/T2."""
    cache_keys = cache_snapshot.cache.keys()
    for od in (T1, T2):
        to_del = [k for k in od.keys() if k not in cache_keys]
        for k in to_del:
            od.pop(k, None)


def _seed_from_cache(cache_snapshot):
    """If segments empty but cache has content, seed T1 with current cache keys."""
    if not T1 and not T2 and cache_snapshot.cache:
        for k0 in cache_snapshot.cache.keys():
            T1[k0] = None


def _touch_last(k: str, now: int):
    _LAST_ACCESS[k] = now


def _lru(od: OrderedDict):
    return next(iter(od)) if od else None


def _sample_cold(od: OrderedDict, now: int):
    """
    Sample first tail_len keys from LRU side and return the coldest by:
    (TinyLFU estimate ascending, last_access_time ascending).
    Returns (key, est).
    """
    if not od:
        return None, None
    tail_len = min(len(od), _SAMPLE_K * _TAIL_MULT)
    it = iter(od.keys())
    best_k, best_est, best_t = None, None, None
    for _ in range(tail_len):
        k = next(it)
        est = SKETCH.estimate(k)
        t = _LAST_ACCESS.get(k, 0)
        if (best_est is None
            or est < best_est
            or (est == best_est and t < best_t)):
            best_k, best_est, best_t = k, est, t
    return best_k, best_est


def evict(cache_snapshot, obj):
    """
    Choose a victim using ARC pressure + TinyLFU tail sampling.
    - Prefer evicting from T1 (probation) unless the incoming key is clearly hotter
      than T2's cold candidate.
    - During scans (long miss streak), evict from T1 when possible to protect T2.
    - Honor ARC rule: if |T1| > P (or key in B2 and |T1| == P), evict from T1.
    """
    _reset_if_new_run(cache_snapshot)
    _prune_metadata(cache_snapshot)
    _seed_from_cache(cache_snapshot)

    now = cache_snapshot.access_count
    cap = max(int(cache_snapshot.capacity), 1)
    k = obj.key

    in_scan = _MISS_STREAK > cap

    cand_t1, f_t1 = _sample_cold(T1, now)
    cand_t2, f_t2 = _sample_cold(T2, now)

    # Scan protection: evict from T1 if possible
    if in_scan and cand_t1 is not None:
        return cand_t1

    # ARC pressure: evict from T1 when it is oversized vs P
    if T1 and ((k in B2 and len(T1) == P) or (len(T1) > P)):
        return cand_t1 if cand_t1 is not None else (cand_t2 if cand_t2 is not None else next(iter(cache_snapshot.cache)))

    # If only one candidate exists
    if cand_t1 is None and cand_t2 is not None:
        return cand_t2
    if cand_t2 is None and cand_t1 is not None:
        return cand_t1

    # Both available: competitive decision with protected bias
    if cand_t1 is not None and cand_t2 is not None:
        f_new = SKETCH.estimate(k)
        # Only replace from T2 if the new key is clearly hotter
        if f_new > (f_t2 or 0) + 1:
            return cand_t2
        # Otherwise evict from probation to protect main hot set
        return cand_t1

    # Fallback: pick any resident key
    return next(iter(cache_snapshot.cache))


def update_after_hit(cache_snapshot, obj):
    """
    On hit:
    - Update TinyLFU and last-access time.
    - T1 hit -> promote to T2 (ARC behavior).
    - T2 hit -> refresh recency.
    - Untracked hit -> place into T2.
    - Reset miss streak.
    """
    _reset_if_new_run(cache_snapshot)
    _prune_metadata(cache_snapshot)

    now = cache_snapshot.access_count
    k = obj.key
    SKETCH.increment(k, 1)
    _touch_last(k, now)

    global _MISS_STREAK
    _MISS_STREAK = 0

    if k in T2:
        # Refresh recency
        T2.move_to_end(k, last=True)
        return
    if k in T1:
        # Promote to protected
        T1.pop(k, None)
        T2[k] = None
        T2.move_to_end(k, last=True)
        return
    # Desync: treat as hot
    T2[k] = None
    T2.move_to_end(k, last=True)


def update_after_insert(cache_snapshot, obj):
    """
    On miss/insertion:
    - Update TinyLFU and last access.
    - ARC p adaptation on ghost hits (damped).
    - Non-ghost admission: TinyLFU competitive decision vs T2 cold tail; bias to T1.
    - Scan-aware: avoid placing scans into T2.
    """
    _reset_if_new_run(cache_snapshot)
    _prune_metadata(cache_snapshot)

    now = cache_snapshot.access_count
    cap = max(int(cache_snapshot.capacity), 1)
    k = obj.key
    SKETCH.increment(k, 1)
    _touch_last(k, now)

    # Remove stale placements
    T1.pop(k, None)
    T2.pop(k, None)

    in_scan = _MISS_STREAK > (cap // 2)

    global P, _MISS_STREAK
    alpha = 0.25  # damping for p-adaptation

    if k in B1:
        # Ghost hit from T1 -> increase P
        delta = max(1, len(B2) // max(1, len(B1)))
        P = min(cap, max(0, int(round(P + alpha * delta))))
        B1.pop(k, None)
        # Favor T2 unless scan suggests caution
        if in_scan and SKETCH.estimate(k) < 3:
            T1[k] = None
            T1.move_to_end(k, last=True)
        else:
            T2[k] = None
            T2.move_to_end(k, last=True)
    elif k in B2:
        # Ghost hit from T2 -> decrease P
        delta = max(1, len(B1) // max(1, len(B2)))
        P = max(0, min(cap, int(round(P - alpha * delta))))
        B2.pop(k, None)
        if in_scan and SKETCH.estimate(k) < 3:
            T1[k] = None
            T1.move_to_end(k, last=True)
        else:
            T2[k] = None
            T2.move_to_end(k, last=True)
    else:
        # Non-ghost admission
        if in_scan:
            # Avoid promoting scans
            T1[k] = None
            T1.move_to_end(k, last=True)
        else:
            f_new = SKETCH.estimate(k)
            if T2:
                cand_t2, f_t2 = _sample_cold(T2, now)
                if f_new > (f_t2 or 0):
                    T2[k] = None
                    T2.move_to_end(k, last=True)
                else:
                    T1[k] = None
                    T1.move_to_end(k, last=True)
            else:
                if f_new >= 2:
                    T2[k] = None
                    T2.move_to_end(k, last=True)
                else:
                    T1[k] = None
                    T1.move_to_end(k, last=True)

    # Update miss streak for scan detection
    _MISS_STREAK += 1

    # Bound ghost sizes
    while (len(B1) + len(B2)) > cap:
        if len(B1) > len(B2):
            B1.popitem(last=False)
        else:
            B2.popitem(last=False)


def update_after_evict(cache_snapshot, obj, evicted_obj):
    """
    After evicting:
    - Move victim into appropriate ghost list (B1 if from T1, else B2).
    - Keep ghost lists bounded.
    """
    _reset_if_new_run(cache_snapshot)

    k = evicted_obj.key
    if k in T1:
        T1.pop(k, None)
        B1.pop(k, None)
        B1[k] = None  # MRU of B1
    elif k in T2:
        T2.pop(k, None)
        B2.pop(k, None)
        B2[k] = None  # MRU of B2

    cap = max(int(cache_snapshot.capacity), 1)
    while (len(B1) + len(B2)) > cap:
        if len(B1) > len(B2):
            B1.popitem(last=False)
        else:
            B2.popitem(last=False)

# EVOLVE-BLOCK-END

# This part remains fixed (not evolved)
def run_caching(trace_path: str, copy_code_dst: str):
    """Run the caching algorithm on a trace"""
    import os
    with open(os.path.abspath(__file__), 'r', encoding="utf-8") as f:
        code_str = f.read()
    with open(os.path.join(copy_code_dst), 'w') as f:
        f.write(code_str)
    from cache_utils import Cache, CacheConfig, CacheObj, Trace
    trace = Trace(trace_path=trace_path)
    cache_capacity = max(int(trace.get_ndv() * 0.1), 1)
    cache = Cache(CacheConfig(cache_capacity))
    for entry in trace.entries:
        obj = CacheObj(key=str(entry.key))
        cache.get(obj)
    with open(copy_code_dst, 'w') as f:
        f.write("")
    hit_rate = round(cache.hit_count / cache.access_count, 6)
    return hit_rate