# EVOLVE-BLOCK-START
"""Window-TinyLFU with SLRU main (Caffeine-style).

Public API:
- evict(cache_snapshot, obj) -> key
- update_after_hit(cache_snapshot, obj)
- update_after_insert(cache_snapshot, obj)
- update_after_evict(cache_snapshot, obj, evicted_obj)
"""

from collections import OrderedDict


class _WTinyLFU_SLRU:
    """
    Windowed TinyLFU + SLRU main cache (metadata only).
    Segments:
      - W: window (recency) LRU
      - M1: main probation (LRU)
      - M2: main protected (LRU)
    Admission:
      - Count-Min Sketch TinyLFU with exponential aging
      - Eviction-time competitive admission W_LRU vs sampled M1 tail
    Promotion:
      - W hit -> M1
      - M1 hit -> M2
      - M2 hit -> recency refresh
    Adaptation:
      - EMA of miss rate drives window fraction and sampling
    """

    def __init__(self):
        # Segments
        self.W = OrderedDict()
        self.M1 = OrderedDict()
        self.M2 = OrderedDict()

        # TinyLFU sketch
        self.depth = 4
        self.width = 0
        self.sketch = []
        self.ops_since_age = 0
        self.age_period = 0

        # Recency timestamps
        self.last_ts = {}

        # Tuning
        self.win_frac = 0.20  # window fraction (of capacity)
        self.prot_frac = 0.80  # fraction of main kept protected
        self.sample_k = 8
        self.bias_m1 = 1  # bias to protect M1 vs W in comparator

        # EMA of miss rate
        self.ema_miss = 0.0
        self.ema_alpha = 0.05

        # Bookkeeping
        self.last_seen_access = -1
        self.plan_promote_key = None  # W candidate to promote to M1 after evicting an M1 victim

    # ---------- utilities ----------

    def _cap(self, cs):
        return max(1, int(cs.capacity))

    def _reset_if_new_run(self, cs):
        if cs.access_count <= 1 or self.last_seen_access > cs.access_count:
            self.W.clear(); self.M1.clear(); self.M2.clear()
            self.width = 0; self.sketch = []; self.ops_since_age = 0; self.age_period = 0
            self.last_ts.clear()
            self.win_frac = 0.20
            self.prot_frac = 0.80
            self.sample_k = 8
            self.bias_m1 = 1
            self.ema_miss = 0.0
            self.plan_promote_key = None
        self.last_seen_access = cs.access_count

    def _ensure_sketch(self, cs):
        if self.width:
            return
        cap = self._cap(cs)
        # width is power of two ≥ 4C (bounded)
        target = max(256, min(1 << 20, 4 * cap))
        w = 1
        while w < target:
            w <<= 1
        self.width = w
        self.sketch = [[0] * self.width for _ in range(self.depth)]
        # Age period between [4C,16C], start mid
        self.age_period = max(4 * cap, min(16 * cap, 8 * cap))
        # Initial sample size
        self.sample_k = max(4, min(12, (cap // 8) or 4))

    def _hash_idx(self, key, i):
        return (hash((key, i, 0x9E3779B97F4A7C15)) & (self.width - 1))

    def _sketch_add(self, cs, key, delta=1):
        self._ensure_sketch(cs)
        for i in range(self.depth):
            self.sketch[i][self._hash_idx(key, i)] += delta
        self.ops_since_age += 1
        if self.ops_since_age >= self.age_period:
            for i in range(self.depth):
                row = self.sketch[i]
                for j in range(self.width):
                    row[j] >>= 1
            self.ops_since_age = 0

    def _sketch_est(self, cs, key):
        self._ensure_sketch(cs)
        est = None
        for i in range(self.depth):
            v = self.sketch[i][self._hash_idx(key, i)]
            est = v if est is None or v < est else est
        return est if est is not None else 0

    def _touch_ts(self, cs, key):
        self.last_ts[key] = cs.access_count

    def _age(self, cs, key):
        # Age = time since last access; larger means older/colder
        last = self.last_ts.get(key)
        if last is None:
            return 1 << 30
        return cs.access_count - last

    def _prune_segments(self, cs):
        ck = cs.cache.keys()
        for seg in (self.W, self.M1, self.M2):
            stale = [k for k in seg.keys() if k not in ck]
            for k in stale:
                seg.pop(k, None)
        # keep timestamps compact
        for k in list(self.last_ts.keys()):
            if k not in ck:
                self.last_ts.pop(k, None)

    def _seed_from_cache(self, cs):
        # If metadata empty but cache has entries, seed M1 (probation)
        if not self.W and not self.M1 and not self.M2 and cs.cache:
            for k in cs.cache.keys():
                self.M1[k] = None

    def _window_target(self, cs):
        cap = self._cap(cs)
        # clamp window fraction between 0.10 and 0.50
        wf = max(0.10, min(0.50, self.win_frac))
        return max(1, int(cap * wf))

    def _protected_target(self, cs):
        # protected target relative to main (M1+M2)
        main = max(1, len(self.M1) + len(self.M2))
        pf = max(0.60, min(0.90, self.prot_frac))
        return max(1, int(main * pf))

    def _ensure_protected_bounds(self, cs):
        # Demote from M2 -> M1 until within target
        target = self._protected_target(cs)
        while len(self.M2) > target:
            k, _ = self.M2.popitem(last=False)
            self.M1[k] = None

    def _sample_tail(self, cs, seg: OrderedDict):
        if not seg:
            return None
        k = min(self.sample_k, len(seg))
        it = iter(seg.keys())  # LRU -> MRU
        best_k, best_f, best_age = None, None, None
        for _ in range(k):
            key = next(it)
            f = self._sketch_est(cs, key)
            age = self._age(cs, key)
            # colder if TinyLFU estimate smaller; tie-break by older age (larger)
            if (best_k is None
                or f < best_f
                or (f == best_f and age > best_age)):
                best_k, best_f, best_age = key, f, age
        return best_k

    def _adjust_adaptation(self, cs):
        # Adapt window size based on EMA miss rate and rough main stability
        # Increase window under heavy misses (scan-like), shrink when stable
        miss = self.ema_miss
        if miss > 0.80:
            self.win_frac = min(0.50, self.win_frac + 0.04)
            self.sample_k = max(4, self.sample_k - 1)  # faster decisions
        elif miss > 0.60:
            self.win_frac = min(0.45, self.win_frac + 0.02)
        elif miss < 0.30 and len(self.M2) > len(self.M1) and len(self.M2) > 0:
            self.win_frac = max(0.12, self.win_frac - 0.01)
            self.sample_k = min(12, self.sample_k + 1)

    # ---------- public API ----------

    def evict(self, cs, obj):
        self._reset_if_new_run(cs)
        self._prune_segments(cs)
        self._ensure_sketch(cs)
        self._seed_from_cache(cs)
        self._adjust_adaptation(cs)

        win_target = self._window_target(cs)

        # Get candidates
        w_lru = next(iter(self.W)) if self.W else None
        m1_cand = self._sample_tail(cs, self.M1) if self.M1 else None

        # Prefer competitive admission when window at/over target
        if self.W and len(self.W) >= win_target and w_lru is not None:
            # Compare W_LRU vs sampled M1
            if m1_cand is None:
                # No M1; evict from W
                self.plan_promote_key = None
                return w_lru
            f_w = self._sketch_est(cs, w_lru)
            f_m1 = self._sketch_est(cs, m1_cand)
            age_w = self._age(cs, w_lru)
            age_m1 = self._age(cs, m1_cand)

            # Decide colder: TinyLFU ascending, tie by older age
            w_colder = (f_w < f_m1) or (f_w == f_m1 and age_w > age_m1 - self.bias_m1)
            if not w_colder and (f_w >= f_m1 + self.bias_m1):
                # Evict M1 candidate; promote W_LRU into M1 after eviction
                self.plan_promote_key = w_lru
                return m1_cand
            else:
                # Evict W_LRU
                self.plan_promote_key = None
                return w_lru

        # Otherwise try to evict from M1 first to let window grow
        if m1_cand is not None:
            self.plan_promote_key = None
            return m1_cand

        # Fallbacks: evict from W; else from M2; else any
        if w_lru is not None:
            self.plan_promote_key = None
            return w_lru
        if self.M2:
            k = self._sample_tail(cs, self.M2) or next(iter(self.M2))
            self.plan_promote_key = None
            return k

        # Final fallback: any key from the cache
        self.plan_promote_key = None
        return next(iter(cs.cache)) if cs.cache else None

    def update_after_hit(self, cs, obj):
        self._reset_if_new_run(cs)
        self._prune_segments(cs)
        self._ensure_sketch(cs)

        k = obj.key
        # EMA miss update with hit=0
        self.ema_miss = (1.0 - self.ema_alpha) * self.ema_miss + self.ema_alpha * 0.0

        # Learn
        self._sketch_add(cs, k, 1)
        self._touch_ts(cs, k)

        # Promotions and recency
        if k in self.M2:
            self.M2.move_to_end(k, last=True)
        elif k in self.M1:
            # Promote to protected
            self.M1.pop(k, None)
            self.M2[k] = None
        elif k in self.W:
            # Refresh in window; if clearly hot, promote to M1
            self.W.move_to_end(k, last=True)
            if self._sketch_est(cs, k) >= 3:
                self.W.pop(k, None)
                self.M1[k] = None
        else:
            # Metadata miss but cache hit: place into main based on estimate
            if self._sketch_est(cs, k) >= 4:
                self.M2[k] = None
            else:
                self.M1[k] = None

        # Keep protected within target
        self._ensure_protected_bounds(cs)

    def update_after_insert(self, cs, obj):
        self._reset_if_new_run(cs)
        self._prune_segments(cs)
        self._ensure_sketch(cs)

        k = obj.key
        # EMA miss update with miss=1
        self.ema_miss = (1.0 - self.ema_alpha) * self.ema_miss + self.ema_alpha * 1.0

        # Learn on admission
        self._sketch_add(cs, k, 1)
        self._touch_ts(cs, k)

        # Remove any stale placements
        self.W.pop(k, None); self.M1.pop(k, None); self.M2.pop(k, None)

        # Admission: bypass to M2 if very hot; else admit to window
        if self._sketch_est(cs, k) >= 5:
            self.M2[k] = None
        else:
            self.W[k] = None

        # Maintain protected size
        self._ensure_protected_bounds(cs)

    def update_after_evict(self, cs, obj, evicted_obj):
        self._reset_if_new_run(cs)
        evk = evicted_obj.key

        # Remove evicted from segments
        if evk in self.W:
            self.W.pop(evk, None)
        elif evk in self.M1:
            self.M1.pop(evk, None)
        elif evk in self.M2:
            self.M2.pop(evk, None)

        # If we planned to promote window LRU in lieu of an M1 victim, do it here
        if self.plan_promote_key is not None and self.plan_promote_key in self.W:
            w = self.plan_promote_key
            self.W.pop(w, None)
            self.M1[w] = None
        self.plan_promote_key = None

        # Keep protected bounds
        self._ensure_protected_bounds(cs)
        # Light adaptation after eviction as well
        self._adjust_adaptation(cs)


# Singleton policy
_policy = _WTinyLFU_SLRU()


def evict(cache_snapshot, obj):
    return _policy.evict(cache_snapshot, obj)


def update_after_hit(cache_snapshot, obj):
    _policy.update_after_hit(cache_snapshot, obj)


def update_after_insert(cache_snapshot, obj):
    _policy.update_after_insert(cache_snapshot, obj)


def update_after_evict(cache_snapshot, obj, evicted_obj):
    _policy.update_after_evict(cache_snapshot, obj, evicted_obj)

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