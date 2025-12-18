# EVOLVE-BLOCK-START
"""Split-Window TinyLFU with dual SLRU main, competitive admission/eviction,
and scan-aware biasing.

Public API:
- evict(cache_snapshot, obj) -> key
- update_after_hit(cache_snapshot, obj)
- update_after_insert(cache_snapshot, obj)
- update_after_evict(cache_snapshot, obj, evicted_obj)
"""

from collections import OrderedDict


class _CmSketch:
    """
    Count-Min Sketch with conservative update and periodic aging (TinyLFU-like).
    - d hash functions, width is a power-of-two.
    - Periodic right-shift halves counters to forget stale history.
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

    def increment(self, key: str, amount: int = 1):
        # Conservative update: increment only counters at the current minimum.
        h = hash(key)
        idxs = [self._hash(h, i) for i in range(self.d)]
        vals = [self.tables[i][idxs[i]] for i in range(self.d)]
        minv = min(vals) if vals else 0
        for i in range(self.d):
            if self.tables[i][idxs[i]] == minv:
                v = self.tables[i][idxs[i]] + amount
                self.tables[i][idxs[i]] = 255 if v > 255 else v
        self._maybe_age()

    def estimate(self, key: str) -> int:
        h = hash(key)
        est = 1 << 30
        for i in range(self.d):
            idx = self._hash(h, i)
            v = self.tables[i][idx]
            if v < est:
                est = v
        return est if est != (1 << 30) else 0


class _SplitWinTLFU:
    """
    Split window + dual SLRU main with TinyLFU admission:
      - Window: W1 (probation) and W2 (protected) for recency.
      - Main:   M1 (probation) and M2 (protected) for frequency.
    Competitive admission and eviction using TinyLFU estimates plus a
    secondary recency signal (last-touch time). Scan-aware cooldown protects
    M2 and suppresses promotions under heavy misses.
    """

    __slots__ = (
        "W1", "W2", "M1", "M2", "capacity",
        "win_frac", "w2_frac", "prot_frac",
        "sketch", "_sample_k",
        "last_touch", "recent", "recent_cap",
        # adaptive stats
        "hits_w", "hits_w2", "hits_main", "prom_m2", "dem_m2",
        "last_tune_time", "tune_period",
        # scan handling
        "miss_streak", "scan_cooldown",
    )

    def __init__(self):
        self.W1 = OrderedDict()
        self.W2 = OrderedDict()
        self.M1 = OrderedDict()
        self.M2 = OrderedDict()
        self.capacity = None
        # segment targets
        self.win_frac = 0.25   # window portion (25%)
        self.w2_frac = 0.30    # within-window protected share
        self.prot_frac = 0.75  # main protected share
        # frequency sketch
        self.sketch = _CmSketch(width_power=12, d=4)
        self._sample_k = 6
        # last-touch timestamps (for tie-break on coldness)
        self.last_touch = {}
        # recent ring (LRU set) for burst detection
        self.recent = OrderedDict()
        self.recent_cap = 0
        # adaptive stats
        self.hits_w = 0
        self.hits_w2 = 0
        self.hits_main = 0
        self.prom_m2 = 0
        self.dem_m2 = 0
        self.last_tune_time = 0
        self.tune_period = 0
        # scan handling
        self.miss_streak = 0
        self.scan_cooldown = 0

    # ----- setup / health -----

    def _ensure_capacity(self, cap: int):
        cap = max(int(cap), 1)
        if self.capacity is None:
            self.capacity = cap
            self._sample_k = max(4, min(12, (cap // 8) or 4))
            # sketch sizing
            try:
                target = max(512, cap * 4)
                wp = max(8, (target - 1).bit_length())
                self.sketch = _CmSketch(width_power=wp, d=4)
                self.sketch.age_period = max(512, min(16384, cap * 8))
            except Exception:
                pass
            self.tune_period = max(256, cap * 4)
            self.last_tune_time = 0
            self.recent_cap = max(128, min(4096, cap))
            self.recent_cap = max(128, min(4096, cap))
            return
        if self.capacity != cap:
            # reset segments on external capacity change
            self.W1.clear(); self.W2.clear(); self.M1.clear(); self.M2.clear()
            self.capacity = cap
            self._sample_k = max(4, min(12, (cap // 8) or 4))
            try:
                target = max(512, cap * 4)
                wp = max(8, (target - 1).bit_length())
                self.sketch = _CmSketch(width_power=wp, d=4)
                self.sketch.age_period = max(512, min(16384, cap * 8))
            except Exception:
                pass
            self.tune_period = max(256, cap * 4)
            self.last_tune_time = 0

    def _self_heal(self, cache_snapshot):
        # Ensure tracked keys align with cache content.
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

    def _targets(self):
        cap = self.capacity or 1
        w_tgt = max(1, int(round(cap * self.win_frac)))
        w2_tgt = min(w_tgt, max(0, int(round(w_tgt * self.w2_frac))))
        main_cap = max(0, cap - w_tgt)
        prot_tgt = min(main_cap, max(0, int(round(main_cap * self.prot_frac))))
        prob_tgt = max(0, main_cap - prot_tgt)
        return w_tgt, w2_tgt, prob_tgt, prot_tgt

    def _maybe_tune(self, now: int):
        # Periodically adapt window/protection based on recent hit shares
        if self.tune_period <= 0:
            return
        if (now - self.last_tune_time) >= self.tune_period:
            # window size
            if self.hits_w > self.hits_main * 1.15:
                self.win_frac = min(0.55, self.win_frac + 0.05)
            elif self.hits_main > self.hits_w * 1.15:
                self.win_frac = max(0.10, self.win_frac - 0.04)
            # W2 share
            if self.hits_w2 > (self.hits_w + 1) * 0.7:
                self.w2_frac = min(0.6, self.w2_frac + 0.05)
            elif self.hits_w > self.hits_w2 * 1.6:
                self.w2_frac = max(0.2, self.w2_frac - 0.05)
            # main protection
            if self.prom_m2 > self.dem_m2 * 1.2 and self.hits_main > self.hits_w:
                self.prot_frac = min(0.9, self.prot_frac + 0.05)
            elif self.dem_m2 > self.prom_m2 * 1.2:
                self.prot_frac = max(0.6, self.prot_frac - 0.05)
            # decay stats
            self.hits_w >>= 1; self.hits_w2 >>= 1; self.hits_main >>= 1
            self.prom_m2 >>= 1; self.dem_m2 >>= 1
            self.last_tune_time = now

    # ----- common ops -----

    def _lru(self, od: OrderedDict):
        return next(iter(od)) if od else None

    def _recent_touch(self, key: str, now: int):
        r = self.recent
        r[key] = now
        r.move_to_end(key)
        # Trim to recent_cap
        while len(r) > self.recent_cap:
            r.popitem(last=False)

    def _touch(self, od: OrderedDict, key: str, now: int):
        od[key] = None
        od.move_to_end(key)
        self.last_touch[key] = now
        self._recent_touch(key, now)

    def _cold_tuple(self, key: str, now: int, bias: int = 0):
        # Lexicographic coldness: lower TinyLFU estimate is colder; if tie, older last-touch (smaller) is colder.
        boost = 1 if key in self.recent else 0
        f = self.sketch.estimate(key) + bias + boost
        lt = self.last_touch.get(key, 0)
        return (f, lt)

    def _sample_lru_cold(self, od: OrderedDict, now: int, bias: int = 0) -> str:
        if not od:
            return None
        k = min(self._sample_k, len(od))
        it = iter(od.keys())  # LRU -> MRU
        best_k, best_t = None, None
        for _ in range(k):
            key = next(it)
            t = self._cold_tuple(key, now, bias=bias)
            if best_t is None or t < best_t:
                best_t, best_k = t, key
                if best_t[0] == 0:
                    break
        return best_k if best_k is not None else self._lru(od)

    # ----- eviction policy -----

    def choose_victim(self, cache_snapshot, new_obj) -> str:
        self._ensure_capacity(cache_snapshot.capacity)
        self._self_heal(cache_snapshot)

        now = cache_snapshot.access_count
        # Cool down scan bias gradually
        if self.scan_cooldown > 0:
            self.scan_cooldown -= 1

        # Candidate selection with frequency-aware tail sampling
        cand_w1 = self._sample_lru_cold(self.W1, now, bias=0)
        cand_m1 = self._sample_lru_cold(self.M1, now, bias=0)
        cand_m2 = self._sample_lru_cold(self.M2, now, bias=1)  # bias protects M2

        f_new = self.sketch.estimate(new_obj.key)
        f_m1 = self.sketch.estimate(cand_m1) if cand_m1 is not None else -1
        f_m2 = self.sketch.estimate(cand_m2) if cand_m2 is not None else -1

        if self.scan_cooldown > 0:
            bias = 4
        else:
            bias = 1

        # Decide competitively against main segments
        replace_m1 = (cand_m1 is not None) and (f_new >= (f_m1 + bias))
        replace_m2 = (self.scan_cooldown == 0) and (cand_m2 is not None) and (f_new >= (f_m2 + bias + 1))

        if replace_m1 and replace_m2:
            # Pick the colder of the two (lexicographic by TinyLFU+recency), with slight M2 protection via bias above
            t_m1 = self._cold_tuple(cand_m1, now, bias=0)
            t_m2 = self._cold_tuple(cand_m2, now, bias=1)
            return cand_m2 if t_m2 < t_m1 else cand_m1
        if replace_m1:
            return cand_m1
        if replace_m2:
            return cand_m2

        # Otherwise protect main and evict from W1 when possible
        if cand_w1 is not None and (len(self.W1) + len(self.W2)) > 0:
            return cand_w1

        # Fallbacks to ensure progress
        if self.M1:
            return self._lru(self.M1)
        if self.M2:
            return self._lru(self.M2)
        if self.W2:
            return self._lru(self.W2)
        return next(iter(cache_snapshot.cache))

    # ----- hit path -----

    def on_hit(self, cache_snapshot, obj):
        self._ensure_capacity(cache_snapshot.capacity)
        now = cache_snapshot.access_count
        key = obj.key
        self.sketch.increment(key, 1)
        self.miss_streak = 0
        if self.scan_cooldown > 0:
            self.scan_cooldown -= 1

        w_tgt, w2_tgt, _, prot_tgt = self._targets()

        if key in self.W1:
            self.hits_w += 1
            # Gate promotion during scan cooldown to avoid polluting W2
            f_est = self.sketch.estimate(key)
            if self.scan_cooldown > 0 and f_est < 2:
                # Refresh in W1
                self._touch(self.W1, key, now)
            else:
                # Promote to window protected
                self.W1.pop(key, None)
                self._touch(self.W2, key, now)
                self.hits_w2 += 1
            # Keep W2 within target (demote cold back to W1)
            while len(self.W2) > w2_tgt:
                demote = self._sample_lru_cold(self.W2, now, bias=0)
                if demote is None:
                    break
                self.W2.pop(demote, None)
                self._touch(self.W1, demote, now)
            self._maybe_tune(now)
            return

        if key in self.W2:
            self.hits_w += 1; self.hits_w2 += 1
            self._touch(self.W2, key, now)
            while len(self.W2) > w2_tgt:
                demote = self._sample_lru_cold(self.W2, now, bias=0)
                if demote is None:
                    break
                self.W2.pop(demote, None)
                self._touch(self.W1, demote, now)
            self._maybe_tune(now)
            return

        if key in self.M1:
            self.hits_main += 1
            # Gate promotion during scan cooldown to avoid polluting M2
            f_est = self.sketch.estimate(key)
            if self.scan_cooldown > 0 and f_est < 2:
                self._touch(self.M1, key, now)
            else:
                # Promote to main protected
                self.M1.pop(key, None)
                self._touch(self.M2, key, now)
                self.prom_m2 += 1
            # Keep M2 within target by demoting cold from M2 to M1
            while len(self.M2) > prot_tgt:
                demote = self._sample_lru_cold(self.M2, now, bias=1)
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

        # Desync: treat as warm and place into M2
        self.hits_main += 1
        self._touch(self.M2, key, now)
        while len(self.M2) > prot_tgt:
            demote = self._sample_lru_cold(self.M2, now, bias=1)
            if demote is None:
                break
            self.M2.pop(demote, None)
            self._touch(self.M1, demote, now)
            self.dem_m2 += 1
        self._maybe_tune(now)

    # ----- insert (miss) path -----

    def on_insert(self, cache_snapshot, obj):
        self._ensure_capacity(cache_snapshot.capacity)
        now = cache_snapshot.access_count
        key = obj.key
        self.sketch.increment(key, 1)

        # Update scan detector for consecutive misses
        self.miss_streak += 1
        cap = self.capacity or 1
        if self.miss_streak > cap:
            # Engage cooldown to protect main from scans
            self.scan_cooldown = max(self.scan_cooldown, cap // 2)
        else:
            if self.scan_cooldown > 0:
                self.scan_cooldown -= 1

        # Ensure it's not tracked elsewhere
        self.W1.pop(key, None); self.W2.pop(key, None)
        self.M1.pop(key, None); self.M2.pop(key, None)

        # Early admission using TinyLFU vs main tails
        f_new = self.sketch.estimate(key)
        cand_m1 = self._sample_lru_cold(self.M1, now, bias=0)
        cand_m2 = self._sample_lru_cold(self.M2, now, bias=1)
        f_m1 = self.sketch.estimate(cand_m1) if cand_m1 is not None else -1
        f_m2 = self.sketch.estimate(cand_m2) if cand_m2 is not None else -1
        bias = 3 if self.scan_cooldown > 0 else 1

        placed = False
        if self.scan_cooldown > 0:
            # Avoid polluting M2; allow M1 for clearly warm items
            if f_new >= 4 or (cand_m1 is not None and f_new >= (f_m1 + bias)):
                self._touch(self.M1, key, now)
                placed = True
        else:
            if (cand_m2 is not None) and ((f_new >= (f_m2 + bias + 1)) or (key in self.recent and f_new >= f_m2)):
                self._touch(self.M2, key, now)
                placed = True
            elif cand_m1 is not None and f_new >= (f_m1 + bias):
                self._touch(self.M1, key, now)
                placed = True

        if not placed:
            # Default probationary admission
            self._touch(self.W1, key, now)

        # Rebalance window size vs target
        w_tgt, w2_tgt, _, prot_tgt = self._targets()

        # If window exceeds target, consider moving W1's LRU to M1 using TinyLFU comparison vs M1 cold tail.
        if (len(self.W1) + len(self.W2)) > w_tgt:
            w1_lru = self._lru(self.W1)
            if w1_lru is not None and w1_lru != key:
                cand_m1 = self._sample_lru_cold(self.M1, now, bias=0)
                f_w1 = self.sketch.estimate(w1_lru)
                f_m1 = self.sketch.estimate(cand_m1) if cand_m1 is not None else -1
                bias = 3 if self.scan_cooldown > 0 else 1
                if f_w1 >= (f_m1 + bias):
                    # Admit into probationary main
                    self.W1.pop(w1_lru, None)
                    self._touch(self.M1, w1_lru, now)
                else:
                    # Keep in window; refresh to avoid immediate churn
                    self._touch(self.W1, w1_lru, now)
            else:
                # If W1 empty (rare), demote a cold W2 back to W1
                demote_w2 = self._sample_lru_cold(self.W2, now, bias=0)
                if demote_w2 is not None:
                    self.W2.pop(demote_w2, None)
                    self._touch(self.W1, demote_w2, now)

        # Keep W2 within its target size by demoting its cold entries to W1
        while len(self.W2) > w2_tgt:
            demote_w2 = self._sample_lru_cold(self.W2, now, bias=0)
            if demote_w2 is None:
                break
            self.W2.pop(demote_w2, None)
            self._touch(self.W1, demote_w2, now)

        # Keep M2 within target (freq-aware demotion)
        while len(self.M2) > prot_tgt:
            demote = self._sample_lru_cold(self.M2, now, bias=1)
            if demote is None:
                break
            self.M2.pop(demote, None)
            self._touch(self.M1, demote, now)
            self.dem_m2 += 1

        # Periodic tuning
        self._maybe_tune(now)

    # ----- evict post-processing -----

    def on_evict(self, cache_snapshot, obj, evicted_obj):
        self._ensure_capacity(cache_snapshot.capacity)
        k = evicted_obj.key
        # Remove evicted key from all segments and metadata
        self.W1.pop(k, None); self.W2.pop(k, None)
        self.M1.pop(k, None); self.M2.pop(k, None)
        self.recent.pop(k, None)
        # last_touch may remain; leave it to be overwritten on reuse


# Singleton policy instance
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