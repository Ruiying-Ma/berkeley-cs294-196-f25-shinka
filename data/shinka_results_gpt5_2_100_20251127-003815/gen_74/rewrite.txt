# EVOLVE-BLOCK-START
"""Window-TinyLFU with Segmented LRU and EMA-based scan/phase guard.

Public API:
- evict(cache_snapshot, obj) -> key
- update_after_hit(cache_snapshot, obj)
- update_after_insert(cache_snapshot, obj)
- update_after_evict(cache_snapshot, obj, evicted_obj)
"""

from collections import OrderedDict, deque


class _CmSketch:
    """
    Compact Count-Min Sketch with conservative aging.
    - d hash functions, width w = 2^p (masking for speed).
    - Halves counters periodically to forget stale history.
    """
    __slots__ = ("d", "w", "tables", "mask", "ops", "age_period", "seeds")

    def __init__(self, width_power=12, d=3):
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
        if est == (1 << 30):
            return 0
        return est


class _WTinyLFUPolicy:
    """
    Window-TinyLFU with Segmented LRU (W, M1, M2) and EMA-driven tuning.

    Segments:
      - W: recency window (new admissions and recency-protected)
      - M1: probationary main (items promoted from W or bypass admission)
      - M2: protected main (frequent items)

    Victim selection:
      - If W exceeds its target, evict from W using TinyLFU-aware tail sampling.
      - Else sample the tails of M1 and M2, choose colder by (TinyLFU asc, age desc),
        with a +1 TinyLFU bias on M2 to make it harder to evict.

    Admission and promotions:
      - Miss insert: default to W; bypass to M1 only if new beats the colder of M1/M2.
      - Hit promotions: W -> M1 (guarded during scan), M1 -> M2, refresh in resident segment.

    Scan/phase guard:
      - EMA of miss rate (alpha ~ 0.05). When high for a window, enter cooldown:
        increases window share, raises promotion/admission thresholds, and prefers evicting from W.
    """

    __slots__ = (
        "W", "M1", "M2",
        "capacity", "win_frac", "prot_frac",
        "sketch",
        "sample_k", "last_touch",
        "ema_miss", "ema_alpha", "cooldown_until",
        "tune_period", "last_tune_at",
        "w_hits", "m1_hits", "m2_hits",
        "recent_out_ring", "recent_out_set",
    )

    def __init__(self):
        self.W = OrderedDict()
        self.M1 = OrderedDict()
        self.M2 = OrderedDict()
        self.capacity = None
        # Default targets: 20% window; main protected 80% of main space
        self.win_frac = 0.20
        self.prot_frac = 0.80
        self.sketch = _CmSketch(width_power=12, d=3)
        self.sample_k = 6
        self.last_touch = {}
        # EMA miss tracking
        self.ema_miss = 0.0
        self.ema_alpha = 0.05
        self.cooldown_until = 0
        # Periodic tuning
        self.tune_period = 1024
        self.last_tune_at = 0
        # Segment hit counts
        self.w_hits = 0
        self.m1_hits = 0
        self.m2_hits = 0
        # Recent-membership ring buffer
        self.recent_out_ring = deque(maxlen=1024)
        self.recent_out_set = set()

    # ---------- Helpers and maintenance ----------

    def _ensure_capacity(self, cap: int):
        if cap is None:
            return
        cap = max(int(cap), 1)
        if self.capacity is None:
            self.capacity = cap
            self.sample_k = max(4, min(12, (cap // 8) or 4))
            self.sketch.age_period = max(512, min(16384, cap * 8))
            self.tune_period = max(512, min(8192, cap * 4))
            self.recent_out_ring = deque(maxlen=max(64, min(4096, cap)))
            return
        if self.capacity != cap:
            # Reinitialize conservatively on capacity change
            self.capacity = cap
            self.W.clear(); self.M1.clear(); self.M2.clear()
            self.sample_k = max(4, min(12, (cap // 8) or 4))
            self.sketch.age_period = max(512, min(16384, cap * 8))
            self.tune_period = max(512, min(8192, cap * 4))
            self.recent_out_ring = deque(maxlen=max(64, min(4096, cap)))
            self.recent_out_set.clear()
            self.last_touch.clear()
            self.ema_miss = 0.0
            self.cooldown_until = 0
            self.w_hits = self.m1_hits = self.m2_hits = 0

    def _prune_stale_residents(self, cache_snapshot):
        cache_keys = cache_snapshot.cache.keys()
        for od in (self.W, self.M1, self.M2):
            for k in list(od.keys()):
                if k not in cache_keys:
                    od.pop(k, None)
        # Prune last_touch for memory control
        if len(self.last_touch) > 4 * max(1, self.capacity):
            for k in list(self.last_touch.keys()):
                if (k not in self.W) and (k not in self.M1) and (k not in self.M2):
                    self.last_touch.pop(k, None)

    def _now(self, cache_snapshot):
        return int(getattr(cache_snapshot, "access_count", 0))

    def _update_last_touch(self, key: str, now: int):
        self.last_touch[key] = now

    def _age(self, key: str, now: int) -> int:
        lt = self.last_touch.get(key, now)
        d = now - lt
        if d < 0:
            d = 0
        if d > 65535:
            d = 65535
        return d

    def _tail_sample_min(self, od: OrderedDict, now: int, k: int) -> str | None:
        if not od:
            return None
        k = min(k, len(od))
        it = iter(od.keys())  # OrderedDict iterates from LRU to MRU
        min_key = None
        min_tuple = None
        for _ in range(k):
            try:
                key = next(it)
            except StopIteration:
                break
            f = self.sketch.estimate(key)
            age = self._age(key, now)
            # coldness tuple: (freq asc, age desc) => (f, -age)
            tup = (f, -age)
            if (min_tuple is None) or (tup < min_tuple):
                min_tuple = tup
                min_key = key
        return min_key if min_key is not None else next(iter(od))

    def _score_coldness(self, key: str, now: int, m2_bias: int = 0):
        f = self.sketch.estimate(key)
        age = self._age(key, now)
        return (f + m2_bias, -age)

    def _record_access_ema(self, miss: bool):
        x = 1.0 if miss else 0.0
        self.ema_miss = (1.0 - self.ema_alpha) * self.ema_miss + self.ema_alpha * x

    def _tune(self, cache_snapshot):
        now = self._now(cache_snapshot)
        if now - self.last_tune_at < self.tune_period:
            return
        self.last_tune_at = now

        # Scan detection and cooldown extension
        if self.ema_miss > 0.80:
            self.cooldown_until = max(self.cooldown_until, now + max(1, self.capacity // 2))

        # Protected main sizing adjustments (prot_frac in [0.60, 0.90])
        total_hits = self.w_hits + self.m1_hits + self.m2_hits
        m2_share = (self.m2_hits / total_hits) if total_hits > 0 else 0.0
        if m2_share > 0.70:
            self.prot_frac = min(0.90, self.prot_frac + 0.05)
        elif self.m1_hits > self.m2_hits * 1.2:
            self.prot_frac = max(0.60, self.prot_frac - 0.05)

        # Window sizing reacts to cooldown and churn
        if now < self.cooldown_until:
            self.win_frac = min(0.40, self.win_frac + 0.05)
        else:
            # settle toward 0.20 if stable
            if m2_share > 0.70 and self.ema_miss < 0.30:
                self.win_frac = max(0.15, self.win_frac - 0.02)
            else:
                self.win_frac = min(0.30, max(0.18, self.win_frac))

        # TinyLFU aging period capacity/phase-aware
        # Push toward shorter aging when EMA is high (more churn)
        target_age = int(max(4 * self.capacity, min(16 * self.capacity,
                          (12 * self.capacity) if m2_share > 0.70 else (6 * self.capacity if self.ema_miss > 0.70 else 8 * self.capacity))))
        self.sketch.age_period = max(512, min(16384, target_age))

        # Tail sampling size adaptive
        if self.ema_miss > 0.70 or self.w_hits > self.m2_hits:
            self.sample_k = 4
        elif m2_share > 0.70:
            self.sample_k = 12
        else:
            self.sample_k = 8

        # Reset hit counters
        self.w_hits = self.m1_hits = self.m2_hits = 0

    def _targets(self):
        cap = self.capacity or 1
        w_target = max(1, int(self.win_frac * cap))
        main = max(0, cap - w_target)
        m2_target = int(self.prot_frac * main)
        if m2_target > main:
            m2_target = main
        return w_target, main, m2_target

    def _in_any(self, key: str) -> bool:
        return key in self.W or key in self.M1 or key in self.M2

    def _touch(self, od: OrderedDict, key: str):
        od[key] = None
        od.move_to_end(key)

    def _promote_to_M1(self, key: str):
        if key in self.W:
            self.W.pop(key, None)
        elif key in self.M2:
            self.M2.pop(key, None)
        self._touch(self.M1, key)

    def _promote_to_M2(self, key: str):
        if key in self.W:
            self.W.pop(key, None)
        if key in self.M1:
            self.M1.pop(key, None)
        self._touch(self.M2, key)

    def _place_in_W(self, key: str):
        # Insert or move to MRU in window
        if key in self.M1:
            self.M1.pop(key, None)
        if key in self.M2:
            self.M2.pop(key, None)
        self._touch(self.W, key)

    # ---------- Victim selection ----------

    def choose_victim(self, cache_snapshot, new_obj) -> str:
        self._ensure_capacity(cache_snapshot.capacity)
        self._prune_stale_residents(cache_snapshot)
        now = self._now(cache_snapshot)
        self._tune(cache_snapshot)

        # Prefer evicting from the window if it exceeds target
        w_target, _, _ = self._targets()
        if len(self.W) > w_target:
            # Randomized tail sampling by taking first k keys from LRU side
            victim = self._tail_sample_min(self.W, now, max(4, min(12, 4 * (self.sample_k // 4))))
            if victim is not None:
                return victim

        # Otherwise, choose colder between M1 and M2 using lexicographic coldness with +1 bias for M2
        cand_m1 = self._tail_sample_min(self.M1, now, self.sample_k) if self.M1 else None
        cand_m2 = self._tail_sample_min(self.M2, now, self.sample_k) if self.M2 else None

        if cand_m1 is None and cand_m2 is None:
            # Fall back to window if available
            if len(self.W) > 0:
                victim = self._tail_sample_min(self.W, now, self.sample_k)
                if victim is not None:
                    return victim
            # Final resort: pick any key from cache snapshot
            return next(iter(cache_snapshot.cache))

        if cand_m1 is None:
            return cand_m2
        if cand_m2 is None:
            return cand_m1

        s1 = self._score_coldness(cand_m1, now, m2_bias=0)
        s2 = self._score_coldness(cand_m2, now, m2_bias=1)  # +1 bias to protect M2
        return cand_m1 if s1 <= s2 else cand_m2

    # ---------- Hooks called by framework ----------

    def on_hit(self, cache_snapshot, obj):
        self._ensure_capacity(cache_snapshot.capacity)
        now = self._now(cache_snapshot)
        key = obj.key

        # Update TinyLFU and EMA (hit => miss=0)
        self.sketch.increment(key, 1)
        self._record_access_ema(miss=False)

        # Maintain resident metadata
        self._prune_stale_residents(cache_snapshot)
        self._update_last_touch(key, now)

        in_cooldown = now < self.cooldown_until

        if key in self.W:
            # Guard promotions during cooldown to avoid promoting scans
            self.w_hits += 1
            if in_cooldown:
                # Require some frequency and competitiveness to promote
                f = self.sketch.estimate(key)
                cand_m1 = self._tail_sample_min(self.M1, now, max(1, self.sample_k // 2))
                thr = self.sketch.estimate(cand_m1) if cand_m1 else 1
                if f >= max(2, thr + 1):
                    self._promote_to_M1(key)
                else:
                    self._touch(self.W, key)
            else:
                # Normal path: W -> M1 on hit
                self._promote_to_M1(key)
        elif key in self.M1:
            self.m1_hits += 1
            # Promote to M2 on hit in probationary
            self._promote_to_M2(key)
        elif key in self.M2:
            self.m2_hits += 1
            self._touch(self.M2, key)
        else:
            # Desync: cache has it, we don't. Treat as hot and place into M2.
            self._promote_to_M2(key)

        self._tune(cache_snapshot)

    def on_insert(self, cache_snapshot, obj):
        self._ensure_capacity(cache_snapshot.capacity)
        now = self._now(cache_snapshot)
        key = obj.key

        # Miss => update TinyLFU and EMA
        self.sketch.increment(key, 1)
        self._record_access_ema(miss=True)

        # Maintain resident metadata
        self._prune_stale_residents(cache_snapshot)
        self._update_last_touch(key, now)

        f_new = self.sketch.estimate(key)
        in_cooldown = now < self.cooldown_until

        # Recent ring buffer membership gives a small boost
        recent_boost = 1 if key in self.recent_out_set else 0
        f_new_eff = f_new + recent_boost

        # Early-bypass rule: bypass W to M1 only if new beats the colder of M1/M2
        cand_m1 = self._tail_sample_min(self.M1, now, self.sample_k) if self.M1 else None
        cand_m2 = self._tail_sample_min(self.M2, now, self.sample_k) if self.M2 else None
        cold_ref = None
        if cand_m1 and cand_m2:
            s1 = self._score_coldness(cand_m1, now, m2_bias=0)
            s2 = self._score_coldness(cand_m2, now, m2_bias=1)
            cold_ref = cand_m1 if s1 <= s2 else cand_m2
        else:
            cold_ref = cand_m1 or cand_m2

        thr_est = self.sketch.estimate(cold_ref) if cold_ref else 0

        if in_cooldown:
            # During cooldown, avoid bypass and fill window
            self._place_in_W(key)
        else:
            # Allow bypass if clearly hotter than both segments' colder candidate
            if f_new_eff >= thr_est + 1:
                self._promote_to_M1(key)
            else:
                self._place_in_W(key)

        # Soft rebalance: if M2 grows too large, demote its LRU to M1
        w_target, main_target, m2_target = self._targets()
        if len(self.M2) > m2_target and len(self.M2) > 0:
            # Demote a cold M2 tail sample to M1 to respect protected size
            demote_key = self._tail_sample_min(self.M2, now, max(1, self.sample_k // 2))
            if demote_key is not None and demote_key in self.M2:
                self.M2.pop(demote_key, None)
                self._touch(self.M1, demote_key)

        # If window is starved vs target and we have room in W, consider moving a cold M1 tail into W
        # (rare; metadata-only rebalancing to keep W active under churn)
        if len(self.W) < w_target and len(self.M1) > 0:
            move_key = self._tail_sample_min(self.M1, now, max(1, self.sample_k // 2))
            if move_key is not None and move_key in self.M1:
                self.M1.pop(move_key, None)
                self._touch(self.W, move_key)

        self._tune(cache_snapshot)

    def on_evict(self, cache_snapshot, obj, evicted_obj):
        self._ensure_capacity(cache_snapshot.capacity)
        self._prune_stale_residents(cache_snapshot)
        evk = evicted_obj.key
        # Remove from any resident segment
        if evk in self.W:
            self.W.pop(evk, None)
        elif evk in self.M1:
            self.M1.pop(evk, None)
        elif evk in self.M2:
            self.M2.pop(evk, None)
        # Record in recent-membership ring buffer
        if evk in self.recent_out_set:
            # refresh recency in ring by re-adding
            try:
                self.recent_out_ring.remove(evk)
            except ValueError:
                pass
        self.recent_out_ring.append(evk)
        self.recent_out_set.add(evk)
        # Enforce ring capacity
        while len(self.recent_out_ring) > self.recent_out_ring.maxlen:
            old = self.recent_out_ring.popleft()
            self.recent_out_set.discard(old)
        # No further action; admission/promotion handled on insert/hit.


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