# EVOLVE-BLOCK-START
"""WTiny-SLRU with TinyLFU-guided competitive admission and adaptive tuning.

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
    - d hash tables, width = 2^p (masking)
    - Periodic halving maintains a decayed frequency
    """
    __slots__ = ("d", "w", "tables", "mask", "ops", "age_period", "seeds")

    def __init__(self, width_power=12, d=3):
        self.d = int(max(1, d))
        w = 1 << int(max(8, width_power))
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


class _WTinySlru:
    """
    Windowed TinyLFU + SLRU:
    - W: window LRU (pure recency)
    - M1: probationary main (admission target)
    - M2: protected main (re-referenced)
    Admission and eviction use TinyLFU competitive k-sampling with M2-biased protection.
    """

    __slots__ = (
        "W", "M1", "M2",
        "capacity", "win_frac", "prot_frac",
        "sketch", "_sample_k",
        "ema_miss", "_alpha_ema", "_cooldown_until", "_over_miss_span",
        "_tune_period", "_last_tune_access",
        "hit_W", "hit_M1", "hit_M2",
        "promotions", "demotions",
        "_last_evicted_from",
        "_recent_ring", "_recent_cap"
    )

    def __init__(self):
        self.W = OrderedDict()
        self.M1 = OrderedDict()
        self.M2 = OrderedDict()
        self.capacity = None
        self.win_frac = 0.20
        self.prot_frac = 0.80
        self.sketch = _CmSketch(width_power=12, d=3)
        self._sample_k = 6
        self.ema_miss = 0.0
        self._alpha_ema = 0.05
        self._cooldown_until = 0
        self._over_miss_span = 0
        self._tune_period = 1024
        self._last_tune_access = 0
        self.hit_W = 0
        self.hit_M1 = 0
        self.hit_M2 = 0
        self.promotions = 0
        self.demotions = 0
        self._last_evicted_from = "W"
        self._recent_ring = OrderedDict()
        self._recent_cap = 1024

    # ---------- capacity/housekeeping ----------

    def _ensure_capacity(self, snapshot):
        cap = max(int(snapshot.capacity), 1)
        if self.capacity is None:
            self.capacity = cap
            self._sample_k = max(4, min(12, (cap // 8) or 4))
            self._tune_period = max(512, cap)
            self._recent_cap = max(64, cap)
            try:
                self.sketch.age_period = max(512, min(16384, cap * 8))
            except Exception:
                pass
            return
        if self.capacity != cap:
            # Reset cleanly on capacity changes to avoid corruption
            self.W.clear(); self.M1.clear(); self.M2.clear()
            self.capacity = cap
            self._sample_k = max(4, min(12, (cap // 8) or 4))
            self._tune_period = max(512, cap)
            self._recent_cap = max(64, cap)
            try:
                self.sketch.age_period = max(512, min(16384, cap * 8))
            except Exception:
                pass

    def _targets(self):
        win_target = max(1, int(self.capacity * self.win_frac))
        main_target = max(1, self.capacity - win_target)
        prot_target = max(1, int(main_target * self.prot_frac))
        return win_target, prot_target

    def _refresh_recent(self, key: str):
        self._recent_ring[key] = None
        self._recent_ring.move_to_end(key)
        if len(self._recent_ring) > self._recent_cap:
            self._recent_ring.popitem(last=False)

    def _prune_desync(self, snapshot):
        # Drop keys not in the actual cache; add any unknown cache keys to W to resync.
        cache_keys = set(snapshot.cache.keys())
        for od in (self.W, self.M1, self.M2):
            for k in list(od.keys()):
                if k not in cache_keys:
                    od.pop(k, None)
        # Repair: any cache key not tracked -> place into W
        tracked = set(self.W.keys()) | set(self.M1.keys()) | set(self.M2.keys())
        for k in cache_keys - tracked:
            self.W[k] = None
            self.W.move_to_end(k)

    # ---------- LRU helpers ----------

    def _touch(self, od: OrderedDict, key: str):
        od[key] = None
        od.move_to_end(key)

    def _pop_lru(self, od: OrderedDict):
        if not od:
            return None
        k, _ = od.popitem(last=False)
        return k

    def _sample_tail_min_freq(self, od: OrderedDict):
        if not od:
            return None, None
        k = min(self._sample_k, len(od))
        it = iter(od.keys())  # from LRU to MRU
        min_key, min_freq = None, None
        for _ in range(k):
            try:
                key = next(it)
            except StopIteration:
                break
            f = self.sketch.estimate(key)
            if min_freq is None or f < min_freq:
                min_key, min_freq = key, f
        if min_key is None:
            key = next(iter(od))
            return key, self.sketch.estimate(key)
        return min_key, min_freq

    # ---------- adaptation ----------

    def _update_ema_on_hit(self):
        self.ema_miss = (1.0 - self._alpha_ema) * self.ema_miss + self._alpha_ema * 0.0
        self._over_miss_span = 0

    def _update_ema_on_miss(self, now):
        self.ema_miss = (1.0 - self._alpha_ema) * self.ema_miss + self._alpha_ema * 1.0
        if self.ema_miss > 0.8:
            self._over_miss_span += 1
            if self._over_miss_span > max(1, self.capacity // 4):
                # Trigger scan cooldown
                self._cooldown_until = now + self.capacity
                # Raise admission barrier during cooldown
                self._over_miss_span = 0
        else:
            self._over_miss_span = 0

    def _maybe_tune(self, snapshot):
        now = snapshot.access_count
        if (now - self._last_tune_access) < self._tune_period:
            return
        self._last_tune_access = now

        total_hits = max(1, self.hit_W + self.hit_M1 + self.hit_M2)
        frac_M2 = self.hit_M2 / total_hits
        frac_M1 = self.hit_M1 / total_hits

        # Adjust protected fraction
        if frac_M2 > 0.70 and self.promotions >= self.demotions:
            self.prot_frac = min(0.90, self.prot_frac + 0.05)
        elif frac_M1 > 0.60:
            self.prot_frac = max(0.60, self.prot_frac - 0.05)

        # Adjust window size slightly based on churn
        if self.ema_miss > 0.6 or frac_M1 + (self.hit_W / total_hits) > 0.7:
            self.win_frac = min(0.35, self.win_frac + 0.05)
        else:
            self.win_frac = max(0.10, self.win_frac - 0.05)

        # Adjust sampling and sketch aging
        if frac_M2 > 0.70:
            self._sample_k = min(12, self._sample_k + 2)
            self.sketch.age_period = max(512, min(16384, self.capacity * 16))
        else:
            self._sample_k = max(4, self._sample_k - 1)
            self.sketch.age_period = max(512, min(16384, self.capacity * 8))

        # During cooldown, temporarily bias towards larger window and smaller M2
        if snapshot.access_count < self._cooldown_until:
            self.win_frac = min(0.45, self.win_frac + 0.10)
            self.prot_frac = max(0.60, self.prot_frac - 0.05)

        # Reset counters
        self.hit_W = self.hit_M1 = self.hit_M2 = 0
        self.promotions = self.demotions = 0

    def _rebalance_after_hit_or_insert(self):
        win_target, prot_target = self._targets()
        # Demote M2 overflow to M1 (frequency-aware demotion: sample tail min)
        while len(self.M2) > prot_target:
            k, _ = self._sample_tail_min_freq(self.M2)
            if k is None:
                break
            self.M2.pop(k, None)
            self.M1[k] = None
            self.M1.move_to_end(k)
            self.demotions += 1
        # No need to strictly bound W or M1 here; eviction will free space.

    # ---------- public API ----------

    def choose_victim(self, snapshot, new_obj) -> str:
        self._ensure_capacity(snapshot)
        self._prune_desync(snapshot)

        now = snapshot.access_count
        in_cooldown = now < self._cooldown_until

        win_target, prot_target = self._targets()

        # If window oversized, evict from W first
        if len(self.W) > win_target and len(self.W) > 0:
            kW, _ = self._sample_tail_min_freq(self.W)
            if kW is not None and kW in snapshot.cache:
                self._last_evicted_from = "W"
                return kW

        # Sample candidates
        kW, fW = self._sample_tail_min_freq(self.W) if self.W else (None, None)
        k1, f1 = self._sample_tail_min_freq(self.M1) if self.M1 else (None, None)
        k2, f2 = self._sample_tail_min_freq(self.M2) if self.M2 else (None, None)
        # Protect M2 with +1 bias
        f2b = (f2 + 1) if f2 is not None else None

        # Estimate incoming object's frequency with small recent-phase boost
        f_new = self.sketch.estimate(new_obj.key)
        if new_obj.key in self._recent_ring:
            f_new += 1

        # Cooldown (scan) bias: prefer evicting from W if possible
        if in_cooldown and kW is not None and kW in snapshot.cache:
            self._last_evicted_from = "W"
            return kW

        # Compare vs main (M1/M2) using TinyLFU with M2 bias
        main_cold = min([v for v in [f1, f2b] if v is not None], default=None)

        # If the new object is not hotter than the main's cold edge, evict from W first, else M1
        if main_cold is not None and f_new <= main_cold:
            if kW is not None and kW in snapshot.cache:
                self._last_evicted_from = "W"
                return kW
            if k1 is not None and k1 in snapshot.cache:
                self._last_evicted_from = "M1"
                return k1
            if k2 is not None and k2 in snapshot.cache:
                self._last_evicted_from = "M2"
                return k2

        # Otherwise, evict from the colder of M1 and M2 with stronger protection for M2
        if k1 is not None and k2 is not None:
            # Evict from M1 unless M2 is clearly colder despite bias (+2 guard)
            if f1 is None:
                victim, seg = k2, "M2"
            elif f2 is None:
                victim, seg = k1, "M1"
            else:
                if f1 <= (f2 + 2):
                    victim, seg = k1, "M1"
                else:
                    victim, seg = k2, "M2"
            if victim in snapshot.cache:
                self._last_evicted_from = seg
                return victim

        # Fall back to any available segment by priority: M1 -> W -> M2
        for seg_name, cand in (("M1", k1), ("W", kW), ("M2", k2)):
            if cand is not None and cand in snapshot.cache:
                self._last_evicted_from = seg_name
                return cand

        # Final fallback: return any key present in cache
        self._last_evicted_from = "W"
        return next(iter(snapshot.cache))

    def on_hit(self, snapshot, obj):
        self._ensure_capacity(snapshot)
        key = obj.key
        self.sketch.increment(key, 1)
        self._update_ema_on_hit()
        self._refresh_recent(key)

        # Update recency/frequency placement
        if key in self.W:
            # Accelerate promotion: any hit in W moves to probation (M1)
            self.W.pop(key, None)
            self._touch(self.M1, key)
            self.promotions += 1
            self.hit_W += 1
        elif key in self.M1:
            self.M1.pop(key, None)
            self._touch(self.M2, key)  # re-reference -> protected
            self.promotions += 1
            self.hit_M1 += 1
        elif key in self.M2:
            self._touch(self.M2, key)
            self.hit_M2 += 1
        else:
            # Desync/repair: assume frequent
            self._touch(self.M2, key)
            self.hit_M2 += 1

        self._rebalance_after_hit_or_insert()
        self._maybe_tune(snapshot)

    def on_insert(self, snapshot, obj):
        self._ensure_capacity(snapshot)
        key = obj.key
        now = snapshot.access_count

        # Update TinyLFU on miss
        self.sketch.increment(key, 1)
        self._update_ema_on_miss(now)
        self._refresh_recent(key)

        in_cooldown = now < self._cooldown_until

        # Competitive admission: compare f(new) against main (M1/M2) with M2 bias
        f_new = self.sketch.estimate(key)
        if key in self._recent_ring:
            f_new += 1  # small phase-shift boost

        kW, fW = self._sample_tail_min_freq(self.W) if self.W else (None, -1)
        k1, f1 = self._sample_tail_min_freq(self.M1) if self.M1 else (None, -1)
        k2, f2 = self._sample_tail_min_freq(self.M2) if self.M2 else (None, -1)
        f2b = (f2 + 1) if f2 is not None and f2 != -1 else None

        # Gate is the colder of M1 and biased M2; if none, fall back to W sample
        gate_candidates = [v for v in [f1, f2b] if v is not None and v != -1]
        gate_main = min(gate_candidates) if gate_candidates else None
        win_target, _ = self._targets()

        if in_cooldown:
            # During cooldown, require a small margin to enter M1
            if gate_main is not None and f_new >= gate_main + 1:
                self._touch(self.M1, key)
            else:
                self._touch(self.W, key)
        else:
            if gate_main is not None:
                if f_new >= gate_main:
                    self._touch(self.M1, key)
                else:
                    # If window already large, admit borderline to M1 to reduce churn
                    if len(self.W) >= win_target and f_new >= (f1 if f1 != -1 else fW):
                        self._touch(self.M1, key)
                    else:
                        self._touch(self.W, key)
            else:
                # No main representatives: fall back to comparing against W
                if f_new >= (fW if fW != -1 else 0):
                    self._touch(self.M1, key)
                else:
                    self._touch(self.W, key)

        self._rebalance_after_hit_or_insert()
        self._maybe_tune(snapshot)

    def on_evict(self, snapshot, obj, evicted_obj):
        self._ensure_capacity(snapshot)
        evk = evicted_obj.key
        # Remove from whichever segment it belongs to
        if evk in self.W:
            self.W.pop(evk, None)
        elif evk in self.M1:
            self.M1.pop(evk, None)
        elif evk in self.M2:
            self.M2.pop(evk, None)
        else:
            # Use last decision as a hint if desynced
            if self._last_evicted_from == "M1":
                self.M1.pop(evk, None)
            elif self._last_evicted_from == "M2":
                self.M2.pop(evk, None)
            else:
                self.W.pop(evk, None)

        # Keep recent ring bounded
        self._recent_ring.pop(evk, None)

        # Occasional tune after eviction as well
        self._maybe_tune(snapshot)


# Single policy instance reused across calls
_policy = _WTinySlru()


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