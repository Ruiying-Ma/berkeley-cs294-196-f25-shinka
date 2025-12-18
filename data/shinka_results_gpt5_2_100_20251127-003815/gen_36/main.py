# EVOLVE-BLOCK-START
"""Regret-driven ARC + conservative TinyLFU hybrid policy.

Public API:
- evict(cache_snapshot, obj) -> key
- update_after_hit(cache_snapshot, obj)
- update_after_insert(cache_snapshot, obj)
- update_after_evict(cache_snapshot, obj, evicted_obj)
"""

from collections import OrderedDict


class _CmSketch:
    """
    Count-Min Sketch with conservative updates and adaptive aging:
    - d hash functions, width w (power-of-two).
    - Conservative increment: only increment counters equal to the current min to reduce noise.
    - Periodically halves counters to age out stale history.
    """
    __slots__ = ("d", "w", "tables", "mask", "ops", "age_period", "seeds")

    def __init__(self, width_power=12, d=4):
        self.d = int(max(1, d))
        w = 1 << int(max(8, width_power))  # at least 256
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

    def estimate(self, key: str) -> int:
        h = hash(key)
        est = 1 << 30
        for i in range(self.d):
            idx = self._hash(h, i)
            v = self.tables[i][idx]
            if v < est:
                est = v
        return est

    def increment(self, key: str, amount: int = 1):
        # Conservative update: increment only counters equal to current min
        h = hash(key)
        idxs = [self._hash(h, i) for i in range(self.d)]
        vals = [self.tables[i][idxs[i]] for i in range(self.d)]
        mn = min(vals)
        for i in range(self.d):
            if self.tables[i][idxs[i]] == mn:
                v = self.tables[i][idxs[i]] + amount
                self.tables[i][idxs[i]] = 255 if v > 255 else v
        self._maybe_age()


class _RegretARCTinyLFU:
    """
    Regret-driven ARC with TinyLFU guidance:
    - T1: probationary (recency), resident LRU
    - T2: protected (frequency), resident LRU
    - B1: ghost of T1 evictions, LRU
    - B2: ghost of T2 evictions, LRU
    - p: target size for T1 (adapts via ghosts and regret feedback)
    - sketch: conservative TinyLFU for global popularity
    - regret_evicted: recent evictions keyed by key -> (time, segment)
    - scan cooldown: protect T2 during scans
    """

    __slots__ = (
        "T1", "T2", "B1", "B2",
        "p", "capacity", "sketch",
        "_sample_k", "_last_evicted_from",
        "miss_streak", "scan_cooldown",
        "regret_evicted", "regret_ttl",
        "_last_seen_access"
    )

    def __init__(self):
        self.T1 = OrderedDict()
        self.T2 = OrderedDict()
        self.B1 = OrderedDict()
        self.B2 = OrderedDict()
        self.p = 0.0
        self.capacity = None
        self.sketch = _CmSketch(width_power=12, d=4)
        self._sample_k = 6
        self._last_evicted_from = 'T1'
        self.miss_streak = 0
        self.scan_cooldown = 0
        self.regret_evicted = {}  # key -> (time, 'T1'|'T2')
        self.regret_ttl = 0
        self._last_seen_access = -1

    # ------------- internal utilities -------------

    def _ensure_capacity(self, cache_snapshot):
        cap = max(int(cache_snapshot.capacity), 1)
        if self.capacity is None or self.capacity != cap:
            # Reset state on capacity changes or first use
            self.T1.clear(); self.T2.clear(); self.B1.clear(); self.B2.clear()
            self.p = 0.0
            self.capacity = cap
            # Sample depth relative to capacity size
            self._sample_k = max(4, min(12, (cap // 8) or 4))
            # Sketch ages proportionally to capacity
            try:
                self.sketch.age_period = max(512, min(16384, cap * 8))
            except Exception:
                pass
            # Regret TTL set to ~half capacity accesses
            self.regret_ttl = max(16, cap // 2)
            self.regret_evicted.clear()
        # Reset if a new run/trace is detected (time restarted)
        if cache_snapshot.access_count <= 1 or self._last_seen_access > cache_snapshot.access_count:
            self.T1.clear(); self.T2.clear(); self.B1.clear(); self.B2.clear()
            self.p = 0.0
            self.miss_streak = 0
            self.scan_cooldown = 0
            self.regret_evicted.clear()
        self._last_seen_access = cache_snapshot.access_count

    def _prune_stale_residents(self, cache_snapshot):
        cache_keys = cache_snapshot.cache.keys()
        for k in list(self.T1.keys()):
            if k not in cache_keys:
                self.T1.pop(k, None)
        for k in list(self.T2.keys()):
            if k not in cache_keys:
                self.T2.pop(k, None)

    def _prune_ghosts(self):
        cap = self.capacity or 1
        # Keep total ghosts <= capacity; evict from the larger side first.
        while (len(self.B1) + len(self.B2)) > cap:
            if len(self.B1) >= len(self.B2):
                self.B1.popitem(last=False)
            else:
                self.B2.popitem(last=False)
        # Remove ghosts that have become resident again
        for k in list(self.B1.keys()):
            if k in self.T1 or k in self.T2:
                self.B1.pop(k, None)
        for k in list(self.B2.keys()):
            if k in self.T1 or k in self.T2:
                self.B2.pop(k, None)

    def _seed_from_cache(self, cache_snapshot):
        if not self.T1 and not self.T2 and cache_snapshot.cache:
            for k in cache_snapshot.cache.keys():
                self.T1[k] = None

    def _touch_T1(self, key: str):
        self.T1[key] = None
        self.T1.move_to_end(key)

    def _touch_T2(self, key: str):
        self.T2[key] = None
        self.T2.move_to_end(key)

    def _move_T1_to_B1(self, key: str):
        self.T1.pop(key, None)
        self.B1[key] = None
        self.B1.move_to_end(key)

    def _move_T2_to_B2(self, key: str):
        self.T2.pop(key, None)
        self.B2[key] = None
        self.B2.move_to_end(key)

    def _sample_lru_min_freq(self, od: OrderedDict) -> str:
        if not od:
            return None
        k = min(self._sample_k, len(od))
        it = iter(od.keys())  # LRU -> MRU
        best_k, best_f = None, None
        for _ in range(k):
            key = next(it)
            f = self.sketch.estimate(key)
            if best_f is None or f < best_f:
                best_f = f
                best_k = key
        return best_k if best_k is not None else next(iter(od))

    def _bound_T2(self):
        # Keep protected from monopolizing: target ~80% of resident tracked keys
        target = max(1, int((self.capacity or 1) * 0.8))
        while len(self.T2) > target:
            demote, _ = self.T2.popitem(last=False)
            self._touch_T1(demote)

    def _apply_regret_on_return(self, now: int, key: str):
        # If the key was recently evicted, adjust p in favor of the other segment
        info = self.regret_evicted.pop(key, None)
        if not info:
            return
        t_evict, seg = info
        if now - t_evict > self.regret_ttl:
            return
        # Adjust p: regret T1 eviction -> increase p (larger T1).
        # Regret T2 eviction -> decrease p (larger T2).
        delta = 1.0
        if seg == 'T1':
            self.p = min(self.capacity, self.p + delta)
        else:
            self.p = max(0.0, self.p - delta)

    # ------------- policy methods -------------

    def choose_victim(self, cache_snapshot, new_obj) -> str:
        self._ensure_capacity(cache_snapshot)
        self._prune_stale_residents(cache_snapshot)
        self._seed_from_cache(cache_snapshot)

        now = cache_snapshot.access_count
        if self.scan_cooldown > 0:
            self.scan_cooldown -= 1

        cand_T1 = self._sample_lru_min_freq(self.T1) if self.T1 else None
        cand_T2 = self._sample_lru_min_freq(self.T2) if self.T2 else None

        # ARC primary rule: if |T1| > p or (new in B2 and |T1| == p), evict from T1
        new_in_B2 = new_obj.key in self.B2
        if self.T1 and (len(self.T1) > int(self.p) or (new_in_B2 and len(self.T1) == int(self.p))):
            self._last_evicted_from = 'T1'
            return cand_T1 if cand_T1 is not None else (cand_T2 or next(iter(cache_snapshot.cache)))

        # Scan-aware bias: prefer evicting from T1 during cooldown
        in_scan = (self.miss_streak > (self.capacity // 2)) or (self.scan_cooldown > 0)
        if in_scan and cand_T1 is not None:
            self._last_evicted_from = 'T1'
            return cand_T1

        # If only one candidate exists, pick it
        if cand_T1 is None and cand_T2 is not None:
            self._last_evicted_from = 'T2'
            return cand_T2
        if cand_T2 is None and cand_T1 is not None:
            self._last_evicted_from = 'T1'
            return cand_T1

        # Both exist: competitive decision using TinyLFU
        if cand_T1 is not None and cand_T2 is not None:
            f_new = self.sketch.estimate(new_obj.key)
            f_t2 = self.sketch.estimate(cand_T2)
            f_t1 = self.sketch.estimate(cand_T1)
            bias = 2 if in_scan else 1
            # If new clearly hotter than T2's cold candidate, replace from T2; else T1
            if f_new > (f_t2 + bias):
                self._last_evicted_from = 'T2'
                return cand_T2
            # Otherwise evict from the colder between T1 and T2; tie -> T1
            if f_t1 <= f_t2:
                self._last_evicted_from = 'T1'
                return cand_T1
            else:
                self._last_evicted_from = 'T2'
                return cand_T2

        # Fallback: pick any key
        self._last_evicted_from = 'T1'
        return next(iter(cache_snapshot.cache))

    def on_hit(self, cache_snapshot, obj):
        self._ensure_capacity(cache_snapshot)
        now = cache_snapshot.access_count
        key = obj.key

        # Frequency update and scan reset
        self.sketch.increment(key, 1)
        self.miss_streak = 0
        if self.scan_cooldown > 0:
            self.scan_cooldown -= 1

        # Regret feedback if this key was evicted recently
        self._apply_regret_on_return(now, key)

        # Remove ghost duplicates of a resident
        self.B1.pop(key, None)
        self.B2.pop(key, None)

        # Promotion gating: avoid T2 pollution, require modest frequency
        promote_thr = 2 if self.scan_cooldown == 0 else 3

        if key in self.T2:
            self._touch_T2(key)
        elif key in self.T1:
            if self.sketch.estimate(key) >= promote_thr:
                self.T1.pop(key, None)
                self._touch_T2(key)
            else:
                self._touch_T1(key)
        else:
            # Desync: assume it's frequent
            self._touch_T2(key)

        # Keep T2 from monopolizing tracked set
        self._bound_T2()
        self._prune_ghosts()

    def on_insert(self, cache_snapshot, obj):
        self._ensure_capacity(cache_snapshot)
        now = cache_snapshot.access_count
        key = obj.key

        # Count miss in frequency
        self.sketch.increment(key, 1)

        # Miss streak and scan detection
        self.miss_streak += 1
        if self.miss_streak > (self.capacity or 1):
            # Enter/extend cooldown
            self.scan_cooldown = max(self.scan_cooldown, self.capacity)

        # Remove any stale placements before placing new
        self.T1.pop(key, None)
        self.T2.pop(key, None)

        # Regret feedback if a recently evicted key returns on miss
        self._apply_regret_on_return(now, key)

        # ARC ghost hits adjust p and admit to T2
        alpha = 0.25  # damp ghost impact
        if key in self.B1:
            delta = max(1, len(self.B2) // max(1, len(self.B1)))
            self.p = min(self.capacity, max(0.0, self.p + alpha * delta))
            self.B1.pop(key, None)
            self._touch_T2(key)
        elif key in self.B2:
            delta = max(1, len(self.B1) // max(1, len(self.B2)))
            self.p = max(0.0, min(self.capacity, self.p - alpha * delta))
            self.B2.pop(key, None)
            self._touch_T2(key)
        else:
            # Non-ghost: TinyLFU competitive admission with hot bypass
            f_new = self.sketch.estimate(key)
            in_scan = self.scan_cooldown > 0
            if f_new >= 5 and not in_scan:
                # Hot bypass to T2
                self._touch_T2(key)
            else:
                if self.T2:
                    cand = self._sample_lru_min_freq(self.T2)
                    f_cand = self.sketch.estimate(cand) if cand is not None else 0
                    if f_new > f_cand and not in_scan:
                        self._touch_T2(key)
                    else:
                        self._touch_T1(key)
                else:
                    # When T2 empty use mild threshold
                    if f_new >= 2 and not in_scan:
                        self._touch_T2(key)
                    else:
                        self._touch_T1(key)

        self._bound_T2()
        self._prune_ghosts()

    def on_evict(self, cache_snapshot, obj, evicted_obj):
        self._ensure_capacity(cache_snapshot)
        now = cache_snapshot.access_count
        k = evicted_obj.key

        # Determine source segment and move to ghost
        seg = None
        if k in self.T1:
            seg = 'T1'
            self._move_T1_to_B1(k)
        elif k in self.T2:
            seg = 'T2'
            self._move_T2_to_B2(k)
        else:
            # fallback to last decision
            seg = self._last_evicted_from
            if seg == 'T1':
                self.B1[k] = None
                self.B1.move_to_end(k)
            else:
                self.B2[k] = None
                self.B2.move_to_end(k)

        # Record for regret learning
        if seg in ('T1', 'T2'):
            self.regret_evicted[k] = (now, seg)

        self._prune_ghosts()


# Single policy instance reused across calls
_policy = _RegretARCTinyLFU()


def evict(cache_snapshot, obj):
    """
    Choose eviction victim key for the incoming obj.
    """
    return _policy.choose_victim(cache_snapshot, obj)


def update_after_hit(cache_snapshot, obj):
    """
    Update policy state after a cache hit on obj.
    """
    _policy.on_hit(cache_snapshot, obj)


def update_after_insert(cache_snapshot, obj):
    """
    Update policy state after a new obj is inserted into the cache.
    """
    _policy.on_insert(cache_snapshot, obj)


def update_after_evict(cache_snapshot, obj, evicted_obj):
    """
    Update policy state after evicting evicted_obj to make room for obj.
    """
    _policy.on_evict(cache_snapshot, obj, evicted_obj)

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