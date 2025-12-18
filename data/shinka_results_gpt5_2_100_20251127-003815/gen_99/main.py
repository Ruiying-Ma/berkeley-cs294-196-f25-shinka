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
        "hits_w", "hits_main", "last_tune_time", "tune_period",
        "miss_streak", "scan_cooldown",
        # Recency and recent-membership tracking
        "last_touch", "recent_ring", "recent_set", "recent_idx", "recent_cap",
        # Hot bypass admission budget
        "hot_bypass_budget", "last_budget_reset", "budget_period",
        # Protected hits for adaptive tuning
        "hits_m2"
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
        # Scan detection
        self.miss_streak = 0
        self.scan_cooldown = 0
        # Recency and recent-membership tracking
        self.last_touch = {}
        self.recent_ring = []
        self.recent_set = set()
        self.recent_idx = 0
        self.recent_cap = 0
        # Hot bypass admission budget
        self.hot_bypass_budget = 0
        self.last_budget_reset = 0
        self.budget_period = 0
        # Protected hits for adaptive tuning
        self.hits_m2 = 0

    # ----- helpers -----

    def _ensure_capacity(self, cap: int):
        if self.capacity is None:
            self.capacity = max(int(cap), 1)
            self._sample_k = max(4, min(12, (self.capacity // 8) or 4))
            # Age faster for smaller caches
            try:
                self.sketch.age_period = max(512, min(16384, self.capacity * 8))
            except Exception:
                pass
            # Set adaptive tuning period relative to capacity
            self.tune_period = max(256, self.capacity * 4)
            self.last_tune_time = 0
            # Recency ring and budget initialization
            self.recent_cap = max(32, min(4096, self.capacity))
            self.recent_ring = [None] * self.recent_cap
            self.recent_set.clear()
            self.recent_idx = 0
            self.budget_period = self.tune_period
            self.hot_bypass_budget = min(8, max(1, int(self.capacity * 0.02)))
            self.last_budget_reset = 0
            return
        if self.capacity != cap:
            # Reset segments if external capacity changes to avoid desync.
            self.W.clear(); self.M1.clear(); self.M2.clear()
            self.capacity = max(int(cap), 1)
            self._sample_k = max(4, min(12, (self.capacity // 8) or 4))
            try:
                self.sketch.age_period = max(512, min(16384, self.capacity * 8))
            except Exception:
                pass
            self.tune_period = max(256, self.capacity * 4)
            self.last_tune_time = 0
            # Reset recency ring and budget with new capacity
            self.recent_cap = max(32, min(4096, self.capacity))
            self.recent_ring = [None] * self.recent_cap
            self.recent_set.clear()
            self.recent_idx = 0
            self.budget_period = self.tune_period
            self.hot_bypass_budget = min(8, max(1, int(self.capacity * 0.02)))
            self.last_budget_reset = 0

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
        # Clean up recency metadata for keys not in cache
        for k in list(self.last_touch.keys()):
            if k not in cache_keys:
                self.last_touch.pop(k, None)
        if self.recent_set:
            for k in list(self.recent_set):
                if k not in cache_keys:
                    # Remove from recent_set; ring will overwrite lazily
                    self.recent_set.discard(k)

    def _maybe_tune(self, now: int):
        # Periodically adapt window size and TinyLFU params.
        if self.tune_period <= 0:
            return
        if (now - self.last_tune_time) >= self.tune_period:
            # Window fraction tuning
            if self.hits_w > self.hits_main * 1.1:
                self.win_frac = min(0.5, self.win_frac + 0.05)
            elif self.hits_main > self.hits_w * 1.1:
                self.win_frac = max(0.05, self.win_frac - 0.05)
            # Adapt sketch aging and sampling k based on M2 dominance
            total_main = max(1, self.hits_main)
            m2_ratio = self.hits_m2 / total_main
            if m2_ratio > 0.7:
                target_age = min(16384, (self.capacity or 1) * 16)
                target_k = 12 if (self.capacity or 0) >= 64 else 10
            elif m2_ratio > 0.4:
                target_age = max(512, (self.capacity or 1) * 8)
                target_k = 8
            else:
                target_age = max(512, (self.capacity or 1) * 4)
                target_k = 6 if m2_ratio > 0.2 else 4
            # Smoothly move toward target parameters
            try:
                self.sketch.age_period = int((self.sketch.age_period * 3 + target_age) // 4)
            except Exception:
                pass
            if self._sample_k < target_k:
                self._sample_k = min(target_k, self._sample_k + 1)
            elif self._sample_k > target_k:
                self._sample_k = max(target_k, self._sample_k - 1)
            # Decay counters and update tune timestamp
            self.hits_w >>= 1
            self.hits_main >>= 1
            self.hits_m2 >>= 1
            self.last_tune_time = now
            # Refresh hot-bypass budget periodically
            self._maybe_reset_budget(now)

    def _lru(self, od: OrderedDict):
        return next(iter(od)) if od else None

    def _touch(self, od: OrderedDict, key: str):
        od[key] = None
        od.move_to_end(key)

    def _get_last_touch(self, key: str) -> int:
        return self.last_touch.get(key, 0)

    def _recent_add(self, key: str):
        # Maintain a small recent-membership structure to accelerate phase shifts.
        if self.recent_cap <= 0:
            return
        if key in self.recent_set:
            return
        if self.recent_ring[self.recent_idx] is not None:
            old = self.recent_ring[self.recent_idx]
            if old in self.recent_set:
                self.recent_set.discard(old)
        self.recent_ring[self.recent_idx] = key
        self.recent_set.add(key)
        self.recent_idx = (self.recent_idx + 1) % self.recent_cap

    def _recent_remove(self, key: str):
        # Opportunistic removal from recent set (ring will overwrite stale entries).
        if key in self.recent_set:
            self.recent_set.discard(key)

    def _sample_tail_min(self, od: OrderedDict) -> str:
        """
        Sample the LRU tail (size ~ 4k) and choose the candidate with minimal
        (frequency, last_touch) lexicographically.
        """
        if not od:
            return None
        tail_len = min(len(od), max(1, self._sample_k * 4))
        it = iter(od.keys())  # from LRU to MRU
        min_key, min_f, min_t = None, None, None
        for _ in range(tail_len):
            key = next(it)
            f = self.sketch.estimate(key)
            t = self._get_last_touch(key)
            if (min_key is None) or (f < min_f) or (f == min_f and t < min_t):
                min_key, min_f, min_t = key, f, t
        return min_key if min_key is not None else self._lru(od)

    def _sample_lru_min_freq(self, od: OrderedDict) -> str:
        # Backward-compatible wrapper
        return self._sample_tail_min(od)

    def _maybe_reset_budget(self, now: int):
        if self.budget_period <= 0:
            return
        if (now - self.last_budget_reset) >= self.budget_period:
            self.hot_bypass_budget = min(8, max(1, int((self.capacity or 1) * 0.02)))
            self.last_budget_reset = now

    # ----- policy decisions -----

    def choose_victim(self, cache_snapshot, new_obj) -> str:
        """
        Dual-segment TinyLFU competitive admission with recency-aware sampling:
        - Sample tails of M1 and M2; choose weakest by (freq + seg_bias, last_touch).
        - If f(new) + recent_boost > f(cand) + bias: evict that candidate (prefer M1).
        - Else evict W's LRU to preserve main from pollution.
        - bias = 1 normally; bias = 3 during scan cooldown to protect main.
        """
        self._ensure_capacity(cache_snapshot.capacity)
        self._self_heal(cache_snapshot)

        # Decay scan cooldown on each miss-driven eviction decision
        if self.scan_cooldown > 0:
            self.scan_cooldown -= 1

        # Candidates from segments
        cand_w = self._lru(self.W)
        cand_m1 = self._sample_tail_min(self.M1)
        cand_m2 = self._sample_tail_min(self.M2)

        # Frequency estimates
        f_new = self.sketch.estimate(new_obj.key)
        # Small recent-membership boost to accelerate phase shifts
        if new_obj.key in self.recent_set:
            f_new += 1

        # Prepare candidate comparison
        def cand_tuple(key, seg_bias=0):
            if key is None:
                return (None, (1 << 30), (1 << 30))  # effectively infinite
            return (key, self.sketch.estimate(key) + seg_bias, self._get_last_touch(key))

        k1, f1, t1 = cand_tuple(cand_m1, 0)
        # Make M2 harder to replace with +1 bias
        extra = 2 if self.scan_cooldown > 0 else 1
        k2, f2, t2 = cand_tuple(cand_m2, extra)

        # Choose weakest between M1 and M2
        choose_m2 = False
        if f2 < f1 or (f2 == f1 and t2 < t1):
            victim_key, victim_freq = k2, f2
            choose_m2 = True
        else:
            victim_key, victim_freq = k1, f1

        bias = 3 if self.scan_cooldown > 0 else 1

        if victim_key is not None and f_new > (victim_freq + bias):
            return victim_key

        # Otherwise evict from the window to preserve main
        if cand_w is not None:
            return cand_w

        # If no window entries, fall back to the chosen main candidate
        if victim_key is not None:
            return victim_key

        # Last resort: pick any key from cache
        return next(iter(cache_snapshot.cache))

    def on_hit(self, cache_snapshot, obj):
        """
        Hit processing:
        - Increment TinyLFU.
        - Maintain last_touch and recent-membership.
        - W hit: refresh or conservatively promote if sufficiently hot.
        - M1 hit: promote to M2.
        - M2 hit: refresh in M2.
        - If untracked but hit (desync): treat as warm and place into M2.
        """
        self._ensure_capacity(cache_snapshot.capacity)
        now = cache_snapshot.access_count
        key = obj.key
        self.sketch.increment(key, 1)

        # Update recency metadata
        self.last_touch[key] = now
        self._recent_add(key)

        # Any hit resets the ongoing miss streak and cools down scan bias
        self.miss_streak = 0
        if self.scan_cooldown > 0:
            self.scan_cooldown -= 1

        if key in self.W:
            self.hits_w += 1
            # Early promotion if strongly frequent to avoid window churn
            est = self.sketch.estimate(key)
            thr = 4 if self.scan_cooldown > 0 else 3
            if est >= thr and self.scan_cooldown == 0:
                # Move from window to protected
                self.W.pop(key, None)
                self._touch(self.M2, key)
                # Keep protected region within target using frequency-aware demotion
                _, _, prot_tgt = self._targets()
                if len(self.M2) > prot_tgt:
                    demote = self._sample_tail_min(self.M2)
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
                demote = self._sample_tail_min(self.M2)
                if demote is not None:
                    self.M2.pop(demote, None)
                    self._touch(self.M1, demote)
            self._maybe_tune(now)
            return

        if key in self.M2:
            self.hits_main += 1
            self.hits_m2 += 1
            self._touch(self.M2, key)
            self._maybe_tune(now)
            return

        # Desync: assume it's warm
        self.hits_main += 1
        self._touch(self.M2, key)
        _, _, prot_tgt = self._targets()
        if len(self.M2) > prot_tgt:
            demote = self._sample_tail_min(self.M2)
            if demote is not None:
                self.M2.pop(demote, None)
                self._touch(self.M1, demote)
        self._maybe_tune(now)

    def on_insert(self, cache_snapshot, obj):
        """
        Insert (on miss) processing:
        - Increment TinyLFU.
        - Insert new key into window W (MRU).
        - Optional hot bypass: direct admission to M2 when new is much hotter than M2 tail.
        - If W exceeds target, TinyLFU-gated move of W's LRU to M1.
        - Keep protected region within target by demoting its LRU to M1 if needed.
        - Maintain scan detector state.
        """
        self._ensure_capacity(cache_snapshot.capacity)
        now = cache_snapshot.access_count
        key = obj.key
        self.sketch.increment(key, 1)

        # Update recency metadata and recent-membership
        self.last_touch[key] = now
        self._recent_add(key)

        # Update scan detector: count consecutive misses
        self.miss_streak += 1
        if self.miss_streak > (self.capacity or 1):
            # Enter/extend scan-biased cooldown
            self.scan_cooldown = max(self.scan_cooldown, self.capacity or 1)
        else:
            # Gradually cool down if not clearly scanning
            if self.scan_cooldown > 0:
                self.scan_cooldown -= 1

        # Ensure it's not tracked elsewhere
        self.W.pop(key, None)
        self.M1.pop(key, None)
        self.M2.pop(key, None)

        # Insert into window
        self._touch(self.W, key)

        # Optional hot-bypass to protected, budget-limited and disabled during cooldown
        self._maybe_reset_budget(now)
        if self.scan_cooldown == 0 and self.hot_bypass_budget > 0:
            f_new = self.sketch.estimate(key)
            cand_m2 = self._sample_tail_min(self.M2)
            f_m2 = self.sketch.estimate(cand_m2) if cand_m2 is not None else -1
            if f_new >= (f_m2 + 2):
                # Directly protect this very hot item
                self.W.pop(key, None)
                self._touch(self.M2, key)
                self.hot_bypass_budget -= 1
                # Keep protected region within target (freq-aware demotion)
                _, _, prot_tgt = self._targets()
                if len(self.M2) > prot_tgt:
                    demote = self._sample_tail_min(self.M2)
                    if demote is not None:
                        self.M2.pop(demote, None)
                        self._touch(self.M1, demote)

        # Rebalance: if W is beyond target, TinyLFU-gated move of W's LRU to M1 (admission path)
        w_tgt, _, prot_tgt = self._targets()
        if len(self.W) > w_tgt:
            w_lru = self._lru(self.W)
            if w_lru is not None and w_lru != key:
                cand_m1 = self._sample_tail_min(self.M1)
                f_w = self.sketch.estimate(w_lru)
                f_m1 = self.sketch.estimate(cand_m1) if cand_m1 is not None else -1
                bias = 3 if self.scan_cooldown > 0 else 1
                # Lexicographic tie-breaker by recency for equal frequency
                if (f_w > (f_m1 + bias)) or (f_w == (f_m1 + bias) and self._get_last_touch(w_lru) < self._get_last_touch(cand_m1) if cand_m1 is not None else True):
                    # Admit into probationary
                    self.W.pop(w_lru, None)
                    self._touch(self.M1, w_lru)
                else:
                    # Keep in window; refresh to MRU to avoid immediate churn
                    self._touch(self.W, w_lru)

        # Keep protected region within target (freq-aware demotion)
        if len(self.M2) > prot_tgt:
            demote = self._sample_tail_min(self.M2)
            if demote is not None:
                self.M2.pop(demote, None)
                self._touch(self.M1, demote)

        # Periodically tune window size and parameters
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
        # Clean recency metadata
        self.last_touch.pop(k, None)
        self._recent_remove(k)


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