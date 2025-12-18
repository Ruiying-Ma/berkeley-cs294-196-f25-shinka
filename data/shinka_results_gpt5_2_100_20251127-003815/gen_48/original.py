# EVOLVE-BLOCK-START
"""W-TinyLFU with windowed recency and SLRU main segments.

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
        # Conservative update: increment only the minimum counters to reduce overestimation.
        h = hash(key)
        idxs = [self._hash(h, i) for i in range(self.d)]
        vals = [self.tables[i][idxs[i]] for i in range(self.d)]
        minv = min(vals) if vals else 0
        for i in range(self.d):
            if self.tables[i][idxs[i]] == minv:
                v = self.tables[i][idxs[i]] + amount
                if v > 255:
                    v = 255
                self.tables[i][idxs[i]] = v
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


class _WTinyLFUPolicy:
    """
    Windowed TinyLFU with SLRU main:
    - W: window LRU (recency buffer).
    - M1: main probationary (first-time in main).
    - M2: main protected (promoted on re-use).
    - TinyLFU sketch for frequency-based admission and candidate selection.
    """

    __slots__ = (
        "W", "M1", "M2", "capacity",
        "win_frac", "prot_frac", "sketch", "_sample_k",
        "hits_w", "hits_main", "last_tune_time", "tune_period"
    )

    def __init__(self):
        self.W = OrderedDict()
        self.M1 = OrderedDict()
        self.M2 = OrderedDict()
        self.capacity = None
        # Targets as fractions of capacity
        self.win_frac = 0.2   # 20% window
        self.prot_frac = 0.8  # 80% of main reserved for protected
        self.sketch = _CmSketch(width_power=12, d=3)
        self._sample_k = 6
        # Adaptive tuning state
        self.hits_w = 0
        self.hits_main = 0
        self.last_tune_time = 0
        self.tune_period = 0

    # ----- helpers -----

    def _ensure_capacity(self, cap: int):
        if self.capacity is None:
            self.capacity = max(int(cap), 1)
            self._sample_k = max(4, min(12, (self.capacity // 8) or 4))
            # Configure TinyLFU sketch width relative to capacity and age period
            try:
                target = max(512, self.capacity * 4)
                wp = max(8, (target - 1).bit_length())
                self.sketch = _CmSketch(width_power=wp, d=4)
                self.sketch.age_period = max(512, min(16384, self.capacity * 8))
            except Exception:
                pass
            # Set adaptive tuning period relative to capacity
            self.tune_period = max(256, self.capacity * 4)
            self.last_tune_time = 0
            return
        if self.capacity != cap:
            # Reset segments if external capacity changes to avoid desync.
            self.W.clear(); self.M1.clear(); self.M2.clear()
            self.capacity = max(int(cap), 1)
            self._sample_k = max(4, min(12, (self.capacity // 8) or 4))
            try:
                target = max(512, self.capacity * 4)
                wp = max(8, (target - 1).bit_length())
                self.sketch = _CmSketch(width_power=wp, d=4)
                self.sketch.age_period = max(512, min(16384, self.capacity * 8))
            except Exception:
                pass
            self.tune_period = max(256, self.capacity * 4)
            self.last_tune_time = 0

    def _targets(self):
        cap = self.capacity or 1
        w_tgt = max(1, int(round(cap * self.win_frac)))
        main_cap = max(0, cap - w_tgt)
        prot_tgt = int(round(main_cap * self.prot_frac))
        prob_tgt = max(0, main_cap - prot_tgt)
        return w_tgt, prob_tgt, prot_tgt

    def _self_heal(self, cache_snapshot):
        # Ensure all cached keys are tracked and no phantom entries remain.
        cache_keys = set(cache_snapshot.cache.keys())
        for od in (self.W, self.M1, self.M2):
            for k in list(od.keys()):
                if k not in cache_keys:
                    od.pop(k, None)
        tracked = set(self.W.keys()) | set(self.M1.keys()) | set(self.M2.keys())
        missing = cache_keys - tracked
        if missing:
            w_tgt, _, _ = self._targets()
            # Place missing into W until target, then into M1
            for k in missing:
                if len(self.W) < w_tgt:
                    self.W[k] = None
                else:
                    self.M1[k] = None

    def _maybe_tune(self, now: int):
        # Periodically adapt window size based on relative hits.
        if self.tune_period <= 0:
            return
        if (now - self.last_tune_time) >= self.tune_period:
            # If window is relatively more useful, grow it; otherwise shrink.
            if self.hits_w > self.hits_main * 1.1:
                self.win_frac = min(0.5, self.win_frac + 0.05)
            elif self.hits_main > self.hits_w * 1.1:
                self.win_frac = max(0.05, self.win_frac - 0.05)
            # Decay counters and update tune timestamp
            self.hits_w >>= 1
            self.hits_main >>= 1
            self.last_tune_time = now

    def _lru(self, od: OrderedDict):
        return next(iter(od)) if od else None

    def _touch(self, od: OrderedDict, key: str):
        od[key] = None
        od.move_to_end(key)

    def _sample_lru_min_freq(self, od: OrderedDict) -> str:
        if not od:
            return None
        k = min(self._sample_k, len(od))
        it = iter(od.keys())  # from LRU to MRU
        min_key, min_f = None, None
        for _ in range(k):
            key = next(it)
            f = self.sketch.estimate(key)
            if min_f is None or f < min_f:
                min_f, min_key = f, key
        return min_key if min_key is not None else self._lru(od)

    # ----- policy decisions -----

    def choose_victim(self, cache_snapshot, new_obj) -> str:
        """
        W-TinyLFU eviction:
        - Compare f(new) to a sampled low-frequency candidate from M1.
          * If f(new) > f(cand_M1): evict cand_M1 (admit new to W).
          * Else: evict W's LRU (reject new from main).
        - If M1 is empty, fall back to comparing against M2; else use window LRU.
        - Robust fallbacks if some segments are empty.
        """
        self._ensure_capacity(cache_snapshot.capacity)
        self._self_heal(cache_snapshot)

        # Candidates
        cand_w = self._lru(self.W)
        cand_m1 = self._sample_lru_min_freq(self.M1)
        cand_m2 = self._sample_lru_min_freq(self.M2) if cand_m1 is None else None

        f_new = self.sketch.estimate(new_obj.key)
        f_m1 = self.sketch.estimate(cand_m1) if cand_m1 is not None else -1
        f_m2 = self.sketch.estimate(cand_m2) if cand_m2 is not None else -1
        bias_m1 = 1
        bias_m2 = 2

        # Prefer replacing a cold M1 entry if new is clearly hotter
        if cand_m1 is not None and f_new > (f_m1 + bias_m1):
            return cand_m1

        # Otherwise evict from the window to preserve main
        if cand_w is not None:
            return cand_w

        # If no window, consider replacing a cold protected entry if new is clearly hotter
        if cand_m2 is not None and f_new > (f_m2 + bias_m2):
            return cand_m2

        # Fallbacks
        if self.M1:
            return self._lru(self.M1)
        if self.M2:
            return self._lru(self.M2)

        # Last resort: pick any key from cache
        return next(iter(cache_snapshot.cache))

    def on_hit(self, cache_snapshot, obj):
        """
        Hit processing:
        - Increment TinyLFU.
        - W hit: refresh or conservatively promote if sufficiently hot.
        - M1 hit: promote to M2.
        - M2 hit: refresh in M2.
        - If untracked but hit (desync): treat as warm and place into M2.
        """
        self._ensure_capacity(cache_snapshot.capacity)
        now = cache_snapshot.access_count
        key = obj.key
        self.sketch.increment(key, 1)

        if key in self.W:
            self.hits_w += 1
            # Early promotion if strongly frequent to avoid window churn
            est = self.sketch.estimate(key)
            if est >= 3:
                # Move from window to protected
                self.W.pop(key, None)
                self._touch(self.M2, key)
                # Keep protected region within target using frequency-aware demotion
                _, _, prot_tgt = self._targets()
                if len(self.M2) > prot_tgt:
                    demote = self._sample_lru_min_freq(self.M2)
                    if demote is not None:
                        self.M2.pop(demote, None)
                        self._touch(self.M1, demote)
            else:
                self._touch(self.W, key)
            self._maybe_tune(now)
            return

        if key in self.M1:
            self.hits_main += 1
            # Promote to protected
            self.M1.pop(key, None)
            self._touch(self.M2, key)
            # Rebalance protected size if needed (freq-aware demotion)
            _, _, prot_tgt = self._targets()
            if len(self.M2) > prot_tgt:
                demote = self._sample_lru_min_freq(self.M2)
                if demote is not None:
                    self.M2.pop(demote, None)
                    self._touch(self.M1, demote)
            self._maybe_tune(now)
            return

        if key in self.M2:
            self.hits_main += 1
            self._touch(self.M2, key)
            self._maybe_tune(now)
            return

        # Desync: assume it's warm
        self.hits_main += 1
        self._touch(self.M2, key)
        _, _, prot_tgt = self._targets()
        if len(self.M2) > prot_tgt:
            demote = self._sample_lru_min_freq(self.M2)
            if demote is not None:
                self.M2.pop(demote, None)
                self._touch(self.M1, demote)
        self._maybe_tune(now)

    def on_insert(self, cache_snapshot, obj):
        """
        Insert (on miss) processing:
        - Increment TinyLFU.
        - Insert new key into window W (MRU).
        - If W exceeds target, move W's LRU to main probationary (M1).
        - Keep protected region within target by demoting its LRU to M1 if needed.
        """
        self._ensure_capacity(cache_snapshot.capacity)
        now = cache_snapshot.access_count
        key = obj.key
        self.sketch.increment(key, 1)

        # Ensure it's not tracked elsewhere
        self.W.pop(key, None)
        self.M1.pop(key, None)
        self.M2.pop(key, None)

        # Insert into window
        self._touch(self.W, key)

        # Rebalance: if W is beyond target, move W's LRU to M1 (admission path)
        w_tgt, _, prot_tgt = self._targets()
        if len(self.W) > w_tgt:
            w_lru = self._lru(self.W)
            if w_lru is not None and w_lru != key:
                # TinyLFU-gated admission into probationary M1
                cand_m1 = self._sample_lru_min_freq(self.M1)
                f_w = self.sketch.estimate(w_lru)
                f_m1 = self.sketch.estimate(cand_m1) if cand_m1 is not None else -1
                if f_w >= (f_m1 + 1):
                    self.W.pop(w_lru, None)
                    self._touch(self.M1, w_lru)
                else:
                    # Keep in window; refresh to MRU to avoid immediate churn
                    self._touch(self.W, w_lru)

        # Keep protected region within target (freq-aware demotion)
        if len(self.M2) > prot_tgt:
            demote = self._sample_lru_min_freq(self.M2)
            if demote is not None:
                self.M2.pop(demote, None)
                self._touch(self.M1, demote)

        # Periodically tune window size
        self._maybe_tune(now)

    def on_evict(self, cache_snapshot, obj, evicted_obj):
        """
        Eviction post-processing:
        - Remove evicted key from whichever segment it resides in.
        """
        self._ensure_capacity(cache_snapshot.capacity)
        k = evicted_obj.key
        self.W.pop(k, None)
        self.M1.pop(k, None)
        self.M2.pop(k, None)


# Single policy instance reused across calls
_policy = _WTinyLFUPolicy()


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