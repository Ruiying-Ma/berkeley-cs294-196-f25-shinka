# EVOLVE-BLOCK-START
"""Split-Window TinyLFU with dual SLRU (window+main), ARC-like promotions,
and scan-aware competitive admission.

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
    - Conservative updates to curb overestimation.
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
        # Conservative update: increment only counters at the current minimum.
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


class _SplitWinTLFU:
    """
    Split-Window TinyLFU with dual SLRU:
    - Window: W1 (probationary), W2 (protected)
    - Main:   M1 (probationary), M2 (protected)
    - TinyLFU for admission and candidate selection
    - Scan-aware bias and adaptive fractions
    """

    __slots__ = (
        "W1", "W2", "M1", "M2", "capacity",
        "win_frac", "w2_frac", "prot_frac",
        "sketch", "_sample_k", "last_touch",
        "hits_w", "hits_main", "hits_w2",
        "prom_m2", "dem_m2",
        "last_tune_time", "tune_period",
        "miss_streak", "scan_cooldown"
    )

    def __init__(self):
        self.W1 = OrderedDict()
        self.W2 = OrderedDict()
        self.M1 = OrderedDict()
        self.M2 = OrderedDict()
        self.capacity = None
        # Fraction targets
        self.win_frac = 0.25   # window portion of cache (25%)
        self.w2_frac = 0.30    # protected portion within window (30% of window)
        self.prot_frac = 0.75  # protected portion within main (75% of main)
        # TinyLFU sketch
        self.sketch = _CmSketch(width_power=12, d=4)
        self._sample_k = 6
        # Last-touch timestamps for lexicographic sampling tie-breakers
        self.last_touch = {}
        # Adaptive state
        self.hits_w = 0
        self.hits_w2 = 0
        self.hits_main = 0
        self.prom_m2 = 0
        self.dem_m2 = 0
        self.last_tune_time = 0
        self.tune_period = 0
        # Scan handling
        self.miss_streak = 0
        self.scan_cooldown = 0

    # ----- helpers -----

    def _ensure_capacity(self, cap: int):
        cap = max(int(cap), 1)
        if self.capacity is None:
            self.capacity = cap
            # sampling and sketch sizing w.r.t. capacity
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
            return
        if self.capacity != cap:
            # External capacity change; reset segments to avoid desync.
            self.W1.clear(); self.W2.clear(); self.M1.clear(); self.M2.clear()
            self.capacity = cap
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
        w2_tgt = min(w_tgt, max(0, int(round(w_tgt * self.w2_frac))))
        main_cap = max(0, cap - w_tgt)
        prot_tgt = min(main_cap, max(0, int(round(main_cap * self.prot_frac))))
        prob_tgt = max(0, main_cap - prot_tgt)
        return w_tgt, w2_tgt, prob_tgt, prot_tgt

    def _self_heal(self, cache_snapshot):
        # Ensure tracked keys align with actual cache content.
        cache_keys = set(cache_snapshot.cache.keys())
        for od in (self.W1, self.W2, self.M1, self.M2):
            for k in list(od.keys()):
                if k not in cache_keys:
                    od.pop(k, None)
        tracked = set().union(self.W1.keys(), self.W2.keys(), self.M1.keys(), self.M2.keys())
        missing = cache_keys - tracked
        if missing:
            # Place missing into W1 until window fills, then M1.
            w_tgt, _, _, _ = self._targets()
            for k in missing:
                if len(self.W1) + len(self.W2) < w_tgt:
                    self.W1[k] = None
                else:
                    self.M1[k] = None

    def _maybe_tune(self, now: int):
        # Periodically adapt window and protection fractions.
        if self.tune_period <= 0:
            return
        if (now - self.last_tune_time) >= self.tune_period:
            # Adapt window size based on relative hit share
            if self.hits_w > self.hits_main * 1.15:
                self.win_frac = min(0.55, self.win_frac + 0.05)
            elif self.hits_main > self.hits_w * 1.15:
                self.win_frac = max(0.08, self.win_frac - 0.04)

            # Adapt W2 share inside window
            if self.hits_w2 > (self.hits_w + 1) * 0.7:
                self.w2_frac = min(0.6, self.w2_frac + 0.05)
            elif self.hits_w > self.hits_w2 * 1.6:
                self.w2_frac = max(0.2, self.w2_frac - 0.05)

            # Adapt main protected fraction based on promotion/demotion balance
            if self.prom_m2 > self.dem_m2 * 1.2 and self.hits_main > self.hits_w:
                self.prot_frac = min(0.9, self.prot_frac + 0.05)
            elif self.dem_m2 > self.prom_m2 * 1.2:
                self.prot_frac = max(0.6, self.prot_frac - 0.05)

            # Decay stats
            self.hits_w >>= 1
            self.hits_w2 >>= 1
            self.hits_main >>= 1
            self.prom_m2 >>= 1
            self.dem_m2 >>= 1
            self.last_tune_time = now

    def _lru(self, od: OrderedDict):
        return next(iter(od)) if od else None

    def _touch(self, od: OrderedDict, key: str, now: int = None):
        od[key] = None
        od.move_to_end(key)
        if now is not None:
            self.last_touch[key] = now

    def _sample_lru_min_freq(self, od: OrderedDict) -> str:
        if not od:
            return None
        # Randomized sampling from the LRU tail, pick minimal (frequency, last_touch).
        import random
        k = min(self._sample_k, len(od))
        tail_len = min(len(od), self._sample_k * 4)
        keys_tail = list(od.keys())[:tail_len]  # LRU-most region
        candidates = keys_tail if tail_len <= k else random.sample(keys_tail, k)
        best_key, best_tuple = None, None
        for key in candidates:
            f = self.sketch.estimate(key)
            lt = self.last_touch.get(key, 0)
            t = (f, lt)
            if best_tuple is None or t < best_tuple:
                best_tuple, best_key = t, key
        return best_key if best_key is not None else self._lru(od)

    # ----- policy decisions -----

    def choose_victim(self, cache_snapshot, new_obj) -> str:
        """
        Dual-main competitive, scan-aware choice:
        - Sample cold candidates from both M1 and M2 tails (M2 protected with +1).
        - If f(new) > min(effective M1/M2) + bias: evict that main candidate.
        - Else evict from W1, then W2; robust fallbacks if empty.
        """
        self._ensure_capacity(cache_snapshot.capacity)
        self._self_heal(cache_snapshot)

        # Cool down scan bias slightly on each eviction decision
        if self.scan_cooldown > 0:
            self.scan_cooldown -= 1

        # Candidates
        cand_w1 = self._lru(self.W1)
        cand_w2 = self._lru(self.W2)
        cand_m1 = self._sample_lru_min_freq(self.M1)
        cand_m2 = self._sample_lru_min_freq(self.M2)

        f_new = self.sketch.estimate(new_obj.key)

        # Compute colder main candidate with M2 bias
        best_cand, best_eff = None, None
        if cand_m1 is not None:
            f1 = self.sketch.estimate(cand_m1)
            best_cand, best_eff = cand_m1, f1
        if cand_m2 is not None:
            f2 = self.sketch.estimate(cand_m2) + 1  # protect M2
            if best_eff is None or f2 < best_eff:
                best_cand, best_eff = cand_m2, f2

        bias = 3 if self.scan_cooldown > 0 else 1

        if best_cand is not None and f_new > (best_eff + bias):
            return best_cand

        # Otherwise, evict from window first
        if cand_w1 is not None:
            return cand_w1
        if cand_w2 is not None:
            return cand_w2

        # Fallbacks
        if self.M1:
            return self._lru(self.M1)
        if self.M2:
            return self._lru(self.M2)
        # Last resort: any key present
        return next(iter(cache_snapshot.cache))

    def on_hit(self, cache_snapshot, obj):
        """
        Hit processing with split window and main SLRU:
        - W1 hit: promote to W2, or directly to M2 if TinyLFU estimate is high.
        - W2 hit: refresh W2.
        - M1 hit: promote to M2 (protected).
        - M2 hit: refresh M2.
        - Desync hit: treat as warm, place in M2.
        """
        self._ensure_capacity(cache_snapshot.capacity)
        now = cache_snapshot.access_count
        key = obj.key
        self.sketch.increment(key, 1)

        # Any hit resets miss streak and cools down scan bias
        self.miss_streak = 0
        if self.scan_cooldown > 0:
            self.scan_cooldown -= 1

        w_tgt, w2_tgt, _, prot_tgt = self._targets()

        if key in self.W1:
            self.hits_w += 1
            # Directly promote to main protected if sufficiently hot
            est = self.sketch.estimate(key)
            thr_hi = 4 if self.scan_cooldown > 0 else 3
            if est >= thr_hi:
                self.W1.pop(key, None)
                self._touch(self.M2, key, now)
                self.hits_main += 1
                self.prom_m2 += 1
                # Keep M2 within target by demoting a low-freq entry to M1
                while len(self.M2) > prot_tgt:
                    demote = self._sample_lru_min_freq(self.M2)
                    if demote is None:
                        break
                    self.M2.pop(demote, None)
                    self._touch(self.M1, demote, now)
                    self.dem_m2 += 1
            else:
                # Otherwise promote to window protected
                self.W1.pop(key, None)
                self._touch(self.W2, key, now)
                self.hits_w2 += 1
                # Keep W2 within target using frequency-aware demotion
                while len(self.W2) > w2_tgt:
                    demote = self._sample_lru_min_freq(self.W2)
                    if demote is None:
                        break
                    self.W2.pop(demote, None)
                    self._touch(self.W1, demote, now)
            self._maybe_tune(now)
            return

        if key in self.W2:
            self.hits_w += 1
            self.hits_w2 += 1
            self._touch(self.W2, key, now)
            # If W2 grew past target, demote coldest to W1
            while len(self.W2) > w2_tgt:
                demote = self._sample_lru_min_freq(self.W2)
                if demote is None:
                    break
                self.W2.pop(demote, None)
                self._touch(self.W1, demote, now)
            self._maybe_tune(now)
            return

        if key in self.M1:
            self.hits_main += 1
            # Promote to main protected
            self.M1.pop(key, None)
            self._touch(self.M2, key, now)
            self.prom_m2 += 1
            # Keep M2 within target by demoting low-freq from M2 to M1
            while len(self.M2) > prot_tgt:
                demote = self._sample_lru_min_freq(self.M2)
                if demote is None:
                    break
                self.M2.pop(demote, None)
                self._touch(self.M1, demote, now)
                self.dem_m2 += 1
            self._maybe_tune(now)
            return

        if key in self.M2:
            self.hits_main += 1
            self._touch(self.M2, key, now)
            self._maybe_tune(now)
            return

        # Desync: assume it's warm and place into M2
        self.hits_main += 1
        self._touch(self.M2, key, now)
        while len(self.M2) > prot_tgt:
            demote = self._sample_lru_min_freq(self.M2)
            if demote is None:
                break
            self.M2.pop(demote, None)
            self._touch(self.M1, demote, now)
            self.dem_m2 += 1
        self._maybe_tune(now)

    def on_insert(self, cache_snapshot, obj):
        """
        Insert (on miss) processing:
        - Increment TinyLFU.
        - Insert new key into W1 (window probationary).
        - Early bypass to M1 when new is clearly hotter than the colder of M1/M2 (M2 +1 bias).
        - If window exceeds target, TinyLFU-gated move of W1's LRU to M1 using dual-main competition.
        - Maintain W2 within its target by frequency-aware demotion to W1.
        - Maintain M2 within its target by frequency-aware demotion to M1.
        - Update scan detector.
        """
        self._ensure_capacity(cache_snapshot.capacity)
        now = cache_snapshot.access_count
        key = obj.key
        self.sketch.increment(key, 1)

        # Update scan detector for consecutive misses with capacity-aware threshold
        self.miss_streak += 1
        thr = max(16, (self.capacity or 1) // 2)
        if self.miss_streak > thr:
            self.scan_cooldown = max(self.scan_cooldown, thr)
        else:
            if self.scan_cooldown > 0:
                self.scan_cooldown -= 1

        # Ensure it's not tracked elsewhere (idempotent)
        self.W1.pop(key, None)
        self.W2.pop(key, None)
        self.M1.pop(key, None)
        self.M2.pop(key, None)

        # Insert into W1 (probationary)
        self._touch(self.W1, key, now)

        # Early bypass: compare against colder of M1 and M2 (+1 bias)
        cand_m1_early = self._sample_lru_min_freq(self.M1)
        cand_m2_early = self._sample_lru_min_freq(self.M2)
        f_new = self.sketch.estimate(key)
        effs = []
        if cand_m1_early is not None:
            effs.append(self.sketch.estimate(cand_m1_early))
        if cand_m2_early is not None:
            effs.append(self.sketch.estimate(cand_m2_early) + 1)
        comp_eff = min(effs) if effs else -1
        bias_early = 3 if self.scan_cooldown > 0 else 1
        thr_hi = 4 if self.scan_cooldown > 0 else 3
        if f_new >= thr_hi and f_new >= (comp_eff + bias_early):
            self.W1.pop(key, None)
            self._touch(self.M1, key, now)

        # Rebalance window size vs target: dual-main, TinyLFU-gated admission
        w_tgt, w2_tgt, _, prot_tgt = self._targets()
        if (len(self.W1) + len(self.W2)) > w_tgt:
            w1_lru = self._lru(self.W1)
            if w1_lru is not None and w1_lru != key:
                cand_m1 = self._sample_lru_min_freq(self.M1)
                cand_m2 = self._sample_lru_min_freq(self.M2)
                f_w1 = self.sketch.estimate(w1_lru)
                comp_list = []
                if cand_m1 is not None:
                    comp_list.append(self.sketch.estimate(cand_m1))
                if cand_m2 is not None:
                    comp_list.append(self.sketch.estimate(cand_m2) + 1)
                comp_eff = min(comp_list) if comp_list else -1
                bias = 3 if self.scan_cooldown > 0 else 1
                if f_w1 >= (comp_eff + bias):
                    # Admit into probationary main
                    self.W1.pop(w1_lru, None)
                    self._touch(self.M1, w1_lru, now)
                else:
                    # Keep in window; refresh to avoid immediate churn
                    self._touch(self.W1, w1_lru, now)
            else:
                # If W1 empty (rare), demote cold W2 back to W1
                demote_w2 = self._sample_lru_min_freq(self.W2)
                if demote_w2 is not None:
                    self.W2.pop(demote_w2, None)
                    self._touch(self.W1, demote_w2, now)

        # Keep W2 within its target size by demoting cold entries to W1
        while len(self.W2) > w2_tgt:
            demote_w2 = self._sample_lru_min_freq(self.W2)
            if demote_w2 is None:
                break
            self.W2.pop(demote_w2, None)
            self._touch(self.W1, demote_w2, now)

        # Keep M2 within target (freq-aware demotion)
        while len(self.M2) > prot_tgt:
            demote = self._sample_lru_min_freq(self.M2)
            if demote is None:
                break
            self.M2.pop(demote, None)
            self._touch(self.M1, demote, now)
            self.dem_m2 += 1

        # Periodically tune parameters
        self._maybe_tune(now)

    def on_evict(self, cache_snapshot, obj, evicted_obj):
        """
        Eviction post-processing:
        - Remove evicted key from all segments.
        """
        self._ensure_capacity(cache_snapshot.capacity)
        k = evicted_obj.key
        self.W1.pop(k, None)
        self.W2.pop(k, None)
        self.M1.pop(k, None)
        self.M2.pop(k, None)
        self.last_touch.pop(k, None)


# Single policy instance reused across calls
_policy = _SplitWinTLFU()


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