# EVOLVE-BLOCK-START
"""Adaptive ARC + TinyLFU (sampled-LFU victim) cache eviction policy.

Public API:
- evict(cache_snapshot, obj) -> key
- update_after_hit(cache_snapshot, obj)
- update_after_insert(cache_snapshot, obj)
- update_after_evict(cache_snapshot, obj, evicted_obj)
"""

from collections import OrderedDict


class _CmSketch:
    """
    Count-Min Sketch with conservative aging.
    - Fixed d hash functions and width w (power of two for masking).
    - Periodically decays counters by right shift to bound growth and forget stale history.
    """
    __slots__ = ("d", "w", "tables", "mask", "ops", "age_period", "seeds")

    def __init__(self, width_power=13, d=4):
        # width = 2^width_power; default 8192; total ints tables = d*width
        self.d = int(max(1, d))
        w = 1 << int(max(8, width_power))  # min width 256
        self.w = w
        self.mask = w - 1
        self.tables = [[0] * w for _ in range(self.d)]
        self.ops = 0
        # Decay roughly every width updates to keep counts in check
        self.age_period = max(2048, w)
        # Fixed seeds for reproducibility
        self.seeds = (0x9e3779b1, 0x85ebca77, 0xc2b2ae3d, 0x27d4eb2f)

    def _hash(self, key_hash: int, i: int) -> int:
        # Mix Python's hash with per-function seed; keep unsigned 64-bit wrap
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
            # Halve all counters (right shift by 1).
            for t in self.tables:
                # Localize for speed
                for i in range(self.w):
                    t[i] >>= 1

    def increment(self, key: str, amount: int = 1):
        h = hash(key)
        for i in range(self.d):
            idx = self._hash(h, i)
            # Increment with a small cap to avoid overflow
            v = self.tables[i][idx] + amount
            # cap at a reasonable small integer to prevent runaway
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


class _ArcTinyLFUPolicy:
    """
    ARC-like resident lists with ghost history + TinyLFU frequency sketch:
    - T1: recency (seen once, resident)
    - T2: frequency (seen >=2 or hot-admitted, resident)
    - B1: ghost for keys evicted from T1
    - B2: ghost for keys evicted from T2
    - p: target size for T1 (adaptive target)
    - sketch: decayed frequency estimator used for admission hints and eviction tie-breaking
    """

    __slots__ = ("T1", "T2", "B1", "B2", "p", "capacity", "_last_evicted_from", "sketch", "_sample_k")

    def __init__(self):
        self.T1 = OrderedDict()
        self.T2 = OrderedDict()
        self.B1 = OrderedDict()
        self.B2 = OrderedDict()
        self.p = 0
        self.capacity = None
        self._last_evicted_from = None
        # Sketch width chosen relative to typical capacities; conservative default
        self.sketch = _CmSketch(width_power=13, d=4)
        # Number of least-recent candidates to sample when evicting from a list
        self._sample_k = 6

    # ---------- internal helpers ----------

    def _ensure_capacity(self, cap: int):
        if self.capacity is None:
            self.capacity = max(int(cap), 1)
            # Size sample proportional to capacity (clamp)
            self._sample_k = max(4, min(12, (self.capacity // 8) or 4))
            # Make sketch aging responsive to capacity (faster aging for small caches)
            try:
                self.sketch.age_period = max(512, min(16384, self.capacity * 8))
            except Exception:
                pass
            return
        if self.capacity != cap:
            # Capacity changed: reset ARC structures to stay consistent.
            self.T1.clear(); self.T2.clear(); self.B1.clear(); self.B2.clear()
            self.p = 0
            self.capacity = max(int(cap), 1)
            self._sample_k = max(4, min(12, (self.capacity // 8) or 4))
            try:
                self.sketch.age_period = max(512, min(16384, self.capacity * 8))
            except Exception:
                pass

    def _prune_stale_residents(self, cache_snapshot):
        cache_keys = set(cache_snapshot.cache.keys())
        for k in list(self.T1.keys()):
            if k not in cache_keys:
                self.T1.pop(k, None)
        for k in list(self.T2.keys()):
            if k not in cache_keys:
                self.T2.pop(k, None)

    def _prune_ghosts(self):
        cap = self.capacity or 1
        # Bound each ghost list to capacity to avoid unbounded growth.
        while len(self.B1) > cap:
            self.B1.popitem(last=False)
        while len(self.B2) > cap:
            self.B2.popitem(last=False)

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
        # Sample from the LRU side: take first k keys, choose one with minimal estimated frequency.
        if not od:
            return None
        k = min(self._sample_k, len(od))
        # OrderedDict iterates from LRU to MRU by default
        it = iter(od.keys())
        min_key = None
        min_freq = None
        for _ in range(k):
            key = next(it)
            f = self.sketch.estimate(key)
            if min_freq is None or f < min_freq:
                min_freq = f
                min_key = key
        if min_key is None:
            # Fallback: simple LRU
            min_key = next(iter(od))
        return min_key

    # ---------- public hooks called by the cache framework ----------

    def choose_victim(self, cache_snapshot, new_obj) -> str:
        """
        ARC + TinyLFU competitive victim selection:
        - If T1 exceeds its target p, evict from T1 (sampled LRU-min-freq).
        - Otherwise, compare TinyLFU(new) vs TinyLFU(candidate_T2) to decide:
          * If new <= candidate_T2, evict from T1 (protect hot main).
          * Else evict from T2 (admit a hotter object).
        - Robust fallbacks if segments are empty or state is desynced.
        """
        self._ensure_capacity(cache_snapshot.capacity)
        self._prune_stale_residents(cache_snapshot)

        # Candidates from segments (min-freq among first k LRU items)
        cand_T1 = self._sample_lru_min_freq(self.T1) if len(self.T1) > 0 else None
        cand_T2 = self._sample_lru_min_freq(self.T2) if len(self.T2) > 0 else None

        # If T1 exceeds target p, prefer evicting from T1 (ARC rule)
        if len(self.T1) > 0 and len(self.T1) > self.p:
            self._last_evicted_from = 'T1'
            return cand_T1 if cand_T1 is not None else next(iter(cache_snapshot.cache))

        # If only one segment has candidates, evict from that segment
        if cand_T1 is None and cand_T2 is not None:
            self._last_evicted_from = 'T2'
            return cand_T2
        if cand_T2 is None and cand_T1 is not None:
            self._last_evicted_from = 'T1'
            return cand_T1

        # Both candidates exist: apply TinyLFU competitive admission heuristic
        if cand_T1 is not None and cand_T2 is not None:
            f_new = self.sketch.estimate(new_obj.key)
            f_t2 = self.sketch.estimate(cand_T2)

            # If incoming is not hotter than T2 candidate, protect main and evict from T1
            if f_new <= f_t2:
                self._last_evicted_from = 'T1'
                return cand_T1
            else:
                self._last_evicted_from = 'T2'
                return cand_T2

        # Fallbacks
        if len(self.T2) > 0:
            self._last_evicted_from = 'T2'
            return self._sample_lru_min_freq(self.T2)
        if len(self.T1) > 0:
            self._last_evicted_from = 'T1'
            return self._sample_lru_min_freq(self.T1)

        # Final resort: pick any key from the actual cache
        self._last_evicted_from = 'T1'
        return next(iter(cache_snapshot.cache))

    def on_hit(self, cache_snapshot, obj):
        """Hit handling: increment frequency; promote to T2 if in T1; reorder within T2 otherwise."""
        self._ensure_capacity(cache_snapshot.capacity)
        key = obj.key
        # Count every access in the sketch
        self.sketch.increment(key, 1)

        if key in self.T1:
            # Second touch promotes to frequency segment
            self.T1.pop(key, None)
            self._touch_T2(key)
        elif key in self.T2:
            # Renew recency in T2
            self._touch_T2(key)
        else:
            # Our state missed it but cache hit: treat as frequent
            self._touch_T2(key)

    def on_insert(self, cache_snapshot, obj):
        """
        Insert handling (called on miss after space made):
        - Increment frequency estimate (counts misses too).
        - If in B1: increase p (bias to recency) and insert into T2.
        - If in B2: decrease p (bias to frequency) and insert into T2.
        - Else: dynamic TinyLFU-based admission:
            Compare f(new) to a sampled LRU-min-freq candidate from T2.
            Hot-admit to T2 only if f(new) > f(candidate_T2); otherwise insert into T1.
        """
        self._ensure_capacity(cache_snapshot.capacity)
        key = obj.key

        # Count this access in sketch (misses included)
        self.sketch.increment(key, 1)

        if key in self.B1:
            # Increase p toward recency; ARC rule
            delta = max(1, len(self.B2) // max(1, len(self.B1)))
            self.p = min(self.capacity, self.p + delta)
            self.B1.pop(key, None)
            self._touch_T2(key)
        elif key in self.B2:
            # Decrease p toward frequency; ARC rule
            delta = max(1, len(self.B1) // max(1, len(self.B2)))
            self.p = max(0, self.p - delta)
            self.B2.pop(key, None)
            self._touch_T2(key)
        else:
            # No ghost history: use dynamic TinyLFU admission threshold
            f_new = self.sketch.estimate(key)
            if len(self.T2) > 0:
                k2 = self._sample_lru_min_freq(self.T2)
                f_k2 = self.sketch.estimate(k2) if k2 is not None else 0
                if f_new > f_k2:
                    self._touch_T2(key)
                else:
                    self._touch_T1(key)
            else:
                # If T2 is empty, fall back to mild threshold
                if f_new >= 2:
                    self._touch_T2(key)
                else:
                    self._touch_T1(key)

        self._prune_ghosts()

    def on_evict(self, cache_snapshot, obj, evicted_obj):
        """
        Eviction handling: move evicted resident to corresponding ghost list.
        """
        self._ensure_capacity(cache_snapshot.capacity)
        evk = evicted_obj.key

        if evk in self.T1:
            self._move_T1_to_B1(evk)
        elif evk in self.T2:
            self._move_T2_to_B2(evk)
        else:
            # Fallback to last chosen segment if our resident state was pruned.
            if self._last_evicted_from == 'T1':
                self.B1[evk] = None
                self.B1.move_to_end(evk)
            else:
                self.B2[evk] = None
                self.B2.move_to_end(evk)

        self._prune_ghosts()


# Single policy instance reused across calls
_policy = _ArcTinyLFUPolicy()


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