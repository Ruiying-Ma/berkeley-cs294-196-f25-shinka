# EVOLVE-BLOCK-START
"""W-TinyLFU+ with window SLRU, conservative sketch, competitive admission, and hot-bypass.

Public API:
- evict(cache_snapshot, obj) -> key
- update_after_hit(cache_snapshot, obj)
- update_after_insert(cache_snapshot, obj)
- update_after_evict(cache_snapshot, obj, evicted_obj)
"""

from collections import OrderedDict


class _CmSketch:
    """
    Count-Min Sketch with conservative update and periodic aging (TinyLFU).
    - d hash functions, width w (power-of-two).
    - Conservative increment: only increment counters at the current minimum.
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
        idxs = [self._hash(h, i) for i in range(self.d)]
        vals = [self.tables[i][idxs[i]] for i in range(self.d)]
        m = min(vals) if vals else 0
        for i in range(self.d):
            if vals[i] == m:
                v = vals[i] + amount
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
    Windowed TinyLFU with:
    - Window split into SLRU: W1 (probationary), W2 (protected).
    - Main SLRU: M1 (probationary), M2 (protected).
    - TinyLFU (conservative) for admission and victim selection.
    - Competitive W->M1 admission and hot-item bypass to M2.
    - Mild adaptive window ramping and sketch aging.
    """

    __slots__ = (
        "W1", "W2", "M1", "M2", "capacity",
        "win_frac", "prot_frac", "sketch", "_sample_k",
        "hits_w", "hits_main", "last_tune_time", "tune_period",
        "baseline_win_frac", "miss_streak"
    )

    def __init__(self):
        self.W1 = OrderedDict()
        self.W2 = OrderedDict()
        self.M1 = OrderedDict()
        self.M2 = OrderedDict()
        self.capacity = None
        self.win_frac = 0.2   # % of cache for window (W1+W2)
        self.prot_frac = 0.8  # % of main for M2 (protected)
        self.baseline_win_frac = self.win_frac
        self.sketch = _CmSketch(width_power=12, d=4)
        self._sample_k = 6
        self.hits_w = 0
        self.hits_main = 0
        self.last_tune_time = 0
        self.tune_period = 0
        self.miss_streak = 0

    # ----- helpers -----

    def _ensure_capacity(self, cap: int):
        cap = max(int(cap), 1)
        if self.capacity is None:
            self.capacity = cap
            # Sample size scales with cache size
            self._sample_k = max(4, min(12, (cap // 8) or 4))
            # Sketch aging scaled to capacity (updated in _maybe_tune)
            try:
                self.sketch.age_period = max(512, min(16384, cap * 8))
            except Exception:
                pass
            self.tune_period = max(256, cap * 4)
            self.last_tune_time = 0
            self.baseline_win_frac = self.win_frac
            return
        if self.capacity != cap:
            # Reset segments on capacity change to avoid desync
            self.W1.clear(); self.W2.clear(); self.M1.clear(); self.M2.clear()
            self.capacity = cap
            self._sample_k = max(4, min(12, (cap // 8) or 4))
            try:
                self.sketch.age_period = max(512, min(16384, cap * 8))
            except Exception:
                pass
            self.tune_period = max(256, cap * 4)
            self.last_tune_time = 0
            self.baseline_win_frac = min(self.baseline_win_frac, 0.6)

    def _targets(self):
        cap = self.capacity or 1
        w_tgt = max(1, int(round(cap * self.win_frac)))
        # Window SLRU split: ~30% to W2
        w2_tgt = min(w_tgt, max(1, int(round(w_tgt * 0.30))))
        main_cap = max(0, cap - w_tgt)
        prot_tgt = int(round(main_cap * self.prot_frac))
        prob_tgt = max(0, main_cap - prot_tgt)
        return w_tgt, w2_tgt, prob_tgt, prot_tgt

    def _self_heal(self, cache_snapshot):
        # Ensure all cached keys are tracked and no phantom entries remain.
        cache_keys = set(cache_snapshot.cache.keys())
        for od in (self.W1, self.W2, self.M1, self.M2):
            for k in list(od.keys()):
                if k not in cache_keys:
                    od.pop(k, None)
        tracked = set(self.W1.keys()) | set(self.W2.keys()) | set(self.M1.keys()) | set(self.M2.keys())
        missing = cache_keys - tracked
        if missing:
            w_tgt, w2_tgt, _, _ = self._targets()
            # Place missing into W1 until target, then into M1
            for k in missing:
                if len(self.W1) + len(self.W2) < w_tgt:
                    # Prefer keeping W2 near its target by filling W1 first
                    self.W1[k] = None
                    if len(self.W2) < w2_tgt and len(self.W1) > 1:
                        # light promotion opportunity
                        mk = next(iter(self.W1))
                        self.W1.pop(mk, None)
                        self.W2[mk] = None
                else:
                    self.M1[k] = None

    def _maybe_tune(self, now: int):
        if self.tune_period <= 0:
            return
        if (now - self.last_tune_time) >= self.tune_period:
            C = self.capacity or 1
            # Ramp when sustained misses and window is comparatively useful
            ramp = (self.miss_streak > (C * 0.5)) and (self.hits_w > (self.hits_main * 1.5))
            if ramp:
                # Increase window temporarily and age TinyLFU faster
                self.win_frac = min(0.60, self.win_frac + 0.10)
                target_age = max(4 * C, 512)
            else:
                # Decay window toward baseline and age TinyLFU slower
                if self.win_frac > self.baseline_win_frac:
                    self.win_frac = max(self.baseline_win_frac, self.win_frac - 0.05)
                target_age = max(12 * C, 1024)
            # Smoothly adapt aging period
            try:
                cur_age = int(self.sketch.age_period)
                self.sketch.age_period = max(256, int(0.5 * cur_age + 0.5 * target_age))
            except Exception:
                pass
            # Adjust tuning cadence
            self.tune_period = max(128, int((2 * C) if ramp else (4 * C)))
            # Half-life decay of hit counters
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
        W-TinyLFU eviction with SLRU window and competitive bias:
        - Prefer displacing a cold M1 entry if f(new) > f(cand_M1) + bias.
        - Else evict from window (W1 LRU first, then W2 LRU).
        - If no window/M1 candidate, consider M2 if f(new) is sufficiently higher.
        - Bias is mildly increased during window ramp to preserve main.
        """
        self._ensure_capacity(cache_snapshot.capacity)
        self._self_heal(cache_snapshot)

        now = cache_snapshot.access_count
        self._maybe_tune(now)

        # Candidates
        cand_w = self._lru(self.W1) or self._lru(self.W2)
        cand_m1 = self._sample_lru_min_freq(self.M1)
        cand_m2 = self._sample_lru_min_freq(self.M2) if cand_m1 is None else None

        f_new = self.sketch.estimate(new_obj.key)
        f_m1 = self.sketch.estimate(cand_m1) if cand_m1 is not None else -1
        f_m2 = self.sketch.estimate(cand_m2) if cand_m2 is not None else -1

        # Mild bias during ramp: be stricter to replace main
        bias = 2 if (self.win_frac > self.baseline_win_frac) else 1

        # Replace a cold M1 if new is hotter
        if cand_m1 is not None and f_new > (f_m1 + bias):
            return cand_m1

        # Otherwise evict from window to preserve main
        if cand_w is not None:
            return cand_w

        # Consider replacing a cold protected entry if clearly hotter
        if cand_m2 is not None and f_new > (f_m2 + bias + 1):
            return cand_m2

        # Fallbacks
        if self.M1:
            return self._lru(self.M1)
        if self.M2:
            return self._lru(self.M2)
        # Last resort
        return next(iter(cache_snapshot.cache))

    def _rebalance_w_slru(self):
        """
        Keep W2 near target by demoting its LRU to W1 when oversized.
        """
        w_tgt, w2_tgt, _, _ = self._targets()
        # Demote from W2 to W1 if W2 exceeds target
        if len(self.W2) > w2_tgt:
            demote = self._lru(self.W2)
            if demote is not None:
                self.W2.pop(demote, None)
                self._touch(self.W1, demote)
        # Keep total window bounded by target by moving from W1 first
        while (len(self.W1) + len(self.W2)) > w_tgt:
            if self.W1:
                self.W1.popitem(last=False)  # Drop W1 LRU from policy (competitive admission handles insert path)
            elif self.W2:
                # Demote W2 LRU into W1, then loop will drop from W1
                demote = self._lru(self.W2)
                if demote is None:
                    break
                self.W2.pop(demote, None)
                self._touch(self.W1, demote)
            else:
                break

    def on_hit(self, cache_snapshot, obj):
        """
        Hit processing:
        - Increment TinyLFU.
        - W1 hit: promote to W2; if very hot, bypass to M2.
        - W2 hit: refresh; if very hot, promote to M2.
        - M1 hit: promote to M2.
        - M2 hit: refresh.
        """
        self._ensure_capacity(cache_snapshot.capacity)
        now = cache_snapshot.access_count
        key = obj.key
        self.sketch.increment(key, 1)

        # Any hit breaks a miss streak
        self.miss_streak = 0

        # Promotion thresholds
        ramp = (self.win_frac > self.baseline_win_frac)
        hot_thr = 4 if ramp else 3  # be stricter during ramp to avoid overprotection

        if key in self.W1:
            self.hits_w += 1
            est = self.sketch.estimate(key)
            if est >= hot_thr:
                # Bypass to protected main
                self.W1.pop(key, None)
                self._touch(self.M2, key)
            else:
                # Promote within window SLRU
                self.W1.pop(key, None)
                self._touch(self.W2, key)
                self._rebalance_w_slru()
            # If M2 too large, demote a cold one into M1
            _, _, _, prot_tgt = self._targets()
            if len(self.M2) > prot_tgt:
                demote = self._sample_lru_min_freq(self.M2)
                if demote is not None:
                    self.M2.pop(demote, None)
                    self._touch(self.M1, demote)
            self._maybe_tune(now)
            return

        if key in self.W2:
            self.hits_w += 1
            est = self.sketch.estimate(key)
            if est >= hot_thr:
                # Promote to protected main
                self.W2.pop(key, None)
                self._touch(self.M2, key)
                _, _, _, prot_tgt = self._targets()
                if len(self.M2) > prot_tgt:
                    demote = self._sample_lru_min_freq(self.M2)
                    if demote is not None:
                        self.M2.pop(demote, None)
                        self._touch(self.M1, demote)
            else:
                self._touch(self.W2, key)
                self._rebalance_w_slru()
            self._maybe_tune(now)
            return

        if key in self.M1:
            self.hits_main += 1
            self.M1.pop(key, None)
            self._touch(self.M2, key)
            _, _, _, prot_tgt = self._targets()
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

        # Desync: treat as warm/protected
        self.hits_main += 1
        self._touch(self.M2, key)
        _, _, _, prot_tgt = self._targets()
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
        - Hot bypass: if est >= 5, place directly into M2 (MRU).
        - Else insert into W1 (MRU).
        - If window exceeds target: competitive admission of W1 LRU into M1,
          comparing f(W1_LRU) against a sampled low-frequency M1 candidate.
          If not admitted, drop the W1 LRU from policy to avoid main pollution.
        - Keep W2 near its target via demotion to W1, and M2 within target via demotion to M1.
        """
        self._ensure_capacity(cache_snapshot.capacity)
        now = cache_snapshot.access_count
        key = obj.key

        # Miss streak tracking for ramp
        self.miss_streak += 1

        # Increment TinyLFU
        self.sketch.increment(key, 1)

        # Ensure it's not tracked elsewhere
        for od in (self.W1, self.W2, self.M1, self.M2):
            od.pop(key, None)

        est_new = self.sketch.estimate(key)
        hot_bypass_thr = 5

        if est_new >= hot_bypass_thr:
            # Bypass window to protected main
            self._touch(self.M2, key)
        else:
            # Insert into window probationary
            self._touch(self.W1, key)

        # Window rebalance with competitive admission
        w_tgt, w2_tgt, _, prot_tgt = self._targets()

        # Keep W2 near target
        if len(self.W2) > w2_tgt:
            demote = self._lru(self.W2)
            if demote is not None:
                self.W2.pop(demote, None)
                self._touch(self.W1, demote)

        # If window oversized, move W1 LRU to M1 only if it's at least as hot
        while (len(self.W1) + len(self.W2)) > w_tgt:
            if self.W1:
                w1_lru = self._lru(self.W1)
                if w1_lru is None:
                    break
                cand_m1 = self._sample_lru_min_freq(self.M1)
                f_w = self.sketch.estimate(w1_lru)
                f_m1 = self.sketch.estimate(cand_m1) if cand_m1 is not None else -1
                # During ramp, require a stronger signal to admit into main
                bias = 1 if (self.win_frac <= self.baseline_win_frac) else 2
                self.W1.pop(w1_lru, None)
                if f_w >= (f_m1 + bias):
                    self._touch(self.M1, w1_lru)
                # else: drop from policy to avoid polluting M1
            elif self.W2:
                # Demote from W2 to W1, loop again to consider admission/drop
                demote = self._lru(self.W2)
                if demote is None:
                    break
                self.W2.pop(demote, None)
                self._touch(self.W1, demote)
            else:
                break

        # Keep protected region within target via frequency-aware demotion
        if len(self.M2) > prot_tgt:
            demote = self._sample_lru_min_freq(self.M2)
            if demote is not None:
                self.M2.pop(demote, None)
                self._touch(self.M1, demote)

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