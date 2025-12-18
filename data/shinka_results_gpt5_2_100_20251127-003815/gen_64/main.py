# EVOLVE-BLOCK-START
"""CSLRU + TinyLFU with dual-tail competitive eviction and phase-aware dynamics.

Public API:
- evict(cache_snapshot, obj) -> key
- update_after_hit(cache_snapshot, obj)
- update_after_insert(cache_snapshot, obj)
- update_after_evict(cache_snapshot, obj, evicted_obj)
"""

from collections import OrderedDict


class _CSLRU_TinyLFU_Compete:
    """
    Three resident segments:
      - W  : Window (recent)       -> LRU
      - M1 : Main probation        -> LRU
      - M2 : Main protected (hot)  -> LRU
    Two ghost lists:
      - G1 : Ghost of W/M1 evictions
      - G2 : Ghost of M2 evictions

    Frequency: TinyLFU (Count-Min Sketch, aged)
    Recency:   LRFU-style exponentially decayed score with adaptive half-life

    Eviction: sample LRU tails of W and M1 and evict the colder entry by
              lexicographic min of (TinyLFU estimate, decayed score).
              Protect M2, but allow evicting a very cold M2 entry if the
              incoming object is clearly hotter.

    Phase-aware:
      - Miss-rate EMA detects scans; during cooldown we freeze M2 promotions
        and raise biases against replacing protected items.
      - TinyLFU aging period and LRFU half-life adapt with EMA.
    """

    __slots__ = (
        # Segments and ghosts
        "W", "M1", "M2", "G1", "G2",
        # TinyLFU (CMS)
        "SKETCH_DEPTH", "sketch_w", "sketch", "sketch_ops", "age_threshold",
        # LRFU
        "score", "last_time", "half_life", "decay_base",
        # Phase / EMA
        "ema_miss", "alpha", "miss_streak", "cooldown_until",
        # Run control
        "last_access_seen",
        # Sampling and sizing
        "sample_k", "win_frac", "prot_frac"
    )

    def __init__(self):
        # Resident segments
        self.W = OrderedDict()
        self.M1 = OrderedDict()
        self.M2 = OrderedDict()
        # Ghosts
        self.G1 = OrderedDict()
        self.G2 = OrderedDict()
        # TinyLFU
        self.SKETCH_DEPTH = 4
        self.sketch_w = 0
        self.sketch = []
        self.sketch_ops = 0
        self.age_threshold = 0
        # LRFU
        self.score = {}
        self.last_time = {}
        self.half_life = 16
        self.decay_base = 2 ** (-1.0 / float(max(1, self.half_life)))
        # Phase/EMA
        self.ema_miss = 0.0
        self.alpha = 0.05
        self.miss_streak = 0
        self.cooldown_until = -1
        # Run
        self.last_access_seen = -1
        # Sampling and sizing targets
        self.sample_k = 8
        self.win_frac = 0.20
        self.prot_frac = 0.70

    # ----- utilities -----

    def _cap(self, cache_snapshot):
        return max(1, int(cache_snapshot.capacity))

    def _reset_if_new_run(self, cache_snapshot):
        if cache_snapshot.access_count <= 1 or self.last_access_seen > cache_snapshot.access_count:
            self.W.clear(); self.M1.clear(); self.M2.clear()
            self.G1.clear(); self.G2.clear()
            self.sketch_w = 0; self.sketch = []; self.sketch_ops = 0; self.age_threshold = 0
            self.score.clear(); self.last_time.clear()
            self.ema_miss = 0.0; self.miss_streak = 0; self.cooldown_until = -1
            self.half_life = 16; self.decay_base = 2 ** (-1.0 / float(self.half_life))
            self.sample_k = 8
            self.win_frac = 0.20
            self.prot_frac = 0.70
        self.last_access_seen = cache_snapshot.access_count

    def _prune_metadata(self, cache_snapshot):
        cache_keys = cache_snapshot.cache.keys()
        for seg in (self.W, self.M1, self.M2):
            stale = [k for k in seg.keys() if k not in cache_keys]
            for k in stale:
                seg.pop(k, None)

    def _seed_from_cache(self, cache_snapshot):
        # If empty metadata but cache has entries, seed into M1
        if not self.W and not self.M1 and not self.M2 and cache_snapshot.cache:
            for k in cache_snapshot.cache.keys():
                self.M1[k] = None

    # ----- TinyLFU CMS -----

    def _ensure_sketch(self, cache_snapshot):
        if self.sketch_w:
            return
        cap = self._cap(cache_snapshot)
        # width: power of two near 4C
        target = max(512, 4 * cap)
        w = 1
        while w < target:
            w <<= 1
        self.sketch_w = w
        self.sketch = [[0] * w for _ in range(self.SKETCH_DEPTH)]
        self.sketch_ops = 0
        # initial age threshold
        self.age_threshold = max(4 * cap, min(16 * cap, 8 * cap))
        # half-life tuned to capacity
        self.half_life = max(8, min(64, (cap // 2) or 8))
        self.decay_base = 2 ** (-1.0 / float(self.half_life))
        # sampling size
        self.sample_k = max(4, min(12, (cap // 8) or 4))

    def _hash_idx(self, key, i):
        return (hash((key, i, 0x9E3779B97F4A7C15)) & (self.sketch_w - 1))

    def _sketch_add(self, cache_snapshot, key, delta=1):
        self._ensure_sketch(cache_snapshot)
        for i in range(self.SKETCH_DEPTH):
            self.sketch[i][self._hash_idx(key, i)] += delta
        self.sketch_ops += 1
        if self.sketch_ops >= self.age_threshold:
            # Global halving
            for i in range(self.SKETCH_DEPTH):
                row = self.sketch[i]
                for j in range(self.sketch_w):
                    row[j] >>= 1
            self.sketch_ops = 0

    def _sketch_est(self, cache_snapshot, key):
        self._ensure_sketch(cache_snapshot)
        est = None
        for i in range(self.SKETCH_DEPTH):
            v = self.sketch[i][self._hash_idx(key, i)]
            est = v if est is None or v < est else est
        return est if est is not None else 0

    # ----- LRFU decayed scores -----

    def _decayed_value(self, key, now):
        lt = self.last_time.get(key)
        if lt is None:
            self.last_time[key] = now
            return self.score.get(key, 0.0)
        dt = now - lt
        if dt > 0:
            self.score[key] = self.score.get(key, 0.0) * (self.decay_base ** dt)
            self.last_time[key] = now
        return self.score.get(key, 0.0)

    def _touch_score(self, key, now, add=1.0):
        v = self._decayed_value(key, now)
        self.score[key] = v + float(add)
        self.last_time[key] = now

    # ----- EMA / phase -----

    def _update_phase(self, cache_snapshot, is_miss):
        cap = self._cap(cache_snapshot)
        # EMA update
        x = 1.0 if is_miss else 0.0
        self.ema_miss = (1.0 - self.alpha) * self.ema_miss + self.alpha * x
        # Miss streak for scan detection
        if is_miss:
            self.miss_streak += 1
        else:
            self.miss_streak = 0
        now = cache_snapshot.access_count

        in_cooldown = now < self.cooldown_until
        # Trigger cooldown when EMA is high and streak is long
        if not in_cooldown and self.ema_miss > 0.8 and self.miss_streak > max(1, cap // 4):
            self.cooldown_until = now + max(1, cap // 4)
            in_cooldown = True
        elif in_cooldown and now >= self.cooldown_until:
            self.cooldown_until = -1
            in_cooldown = False

        # Adapt TinyLFU aging and LRFU half-life with EMA
        # More churn -> smaller thresholds/half-life
        age_lo, age_hi = 4 * cap, 16 * cap
        self.age_threshold = int(age_lo + (1.0 - min(1.0, self.ema_miss)) * (age_hi - age_lo))
        hl_lo, hl_hi = 16, 64
        target_hl = int(hl_lo + (1.0 - min(1.0, self.ema_miss)) * (hl_hi - hl_lo))
        target_hl = max(8, min(64, target_hl))
        self.half_life = target_hl
        self.decay_base = 2 ** (-1.0 / float(self.half_life))

        # Segment sizing tweaks during cooldown
        if in_cooldown:
            self.prot_frac = 0.65
            self.win_frac = 0.25
        else:
            self.prot_frac = 0.70
            self.win_frac = 0.20

    # ----- helpers for segments -----

    def _move_to_mru(self, seg, key):
        if key in seg:
            seg.move_to_end(key, last=True)
        else:
            seg[key] = None

    def _trim_ghosts(self, cache_snapshot):
        cap = self._cap(cache_snapshot)
        # Bound combined ghosts to 2C
        while len(self.G1) + len(self.G2) > 2 * cap:
            if len(self.G1) > len(self.G2):
                self.G1.popitem(last=False)
            else:
                self.G2.popitem(last=False)

    def _enforce_targets(self, cache_snapshot):
        cap = self._cap(cache_snapshot)
        win_target = max(1, int(self.win_frac * cap))
        prot_target = max(1, int(self.prot_frac * cap))
        # Demote M2 if oversized
        while len(self.M2) > prot_target:
            k, _ = self.M2.popitem(last=False)
            self.M1[k] = None
        # Push W overflow to M1
        while len(self.W) > win_target:
            k, _ = self.W.popitem(last=False)
            self.M1[k] = None

    # ----- coldness comparator and sampling -----

    def _cold_tuple(self, cache_snapshot, key, m2_bias=0):
        # Lexicographic coldness: (TinyLFU est, decayed score)
        # Higher bias protects M2 (raises its apparent frequency).
        f = self._sketch_est(cache_snapshot, key) + m2_bias
        d = self._decayed_value(key, cache_snapshot.access_count)
        return (f, d)

    def _sample_cold(self, cache_snapshot, seg, k=None, m2_bias=0):
        if not seg:
            return None
        now = cache_snapshot.access_count  # touch decay passively
        # sample from LRU side
        sample_k = min(k if k is not None else self.sample_k, len(seg))
        it = iter(seg.keys())
        best_key, best_tuple = None, None
        for _ in range(sample_k):
            key = next(it)
            # update decay view to now
            _ = self._decayed_value(key, now)
            ct = self._cold_tuple(cache_snapshot, key, m2_bias=m2_bias)
            if (best_tuple is None) or (ct < best_tuple):
                best_tuple, best_key = ct, key
                if best_tuple[0] == 0 and best_tuple[1] == 0.0:
                    break
        return best_key

    # ----- admission and promotion rules -----

    def _admit_segment(self, cache_snapshot, key, f_new):
        # Competitive admission:
        # Prefer M2 if clearly hot; else compete vs M1 tail; else W.
        if f_new >= 4:
            self._move_to_mru(self.M2, key)
            return
        cand_m1 = self._sample_cold(cache_snapshot, self.M1)
        f_m1 = self._sketch_est(cache_snapshot, cand_m1) if cand_m1 is not None else -1
        if f_new >= f_m1 + 1:
            self._move_to_mru(self.M1, key)
        else:
            self._move_to_mru(self.W, key)

    # ----- public API -----

    def evict(self, cache_snapshot, obj):
        self._reset_if_new_run(cache_snapshot)
        self._prune_metadata(cache_snapshot)
        self._ensure_sketch(cache_snapshot)
        self._seed_from_cache(cache_snapshot)
        self._enforce_targets(cache_snapshot)

        now = cache_snapshot.access_count
        in_cooldown = now < self.cooldown_until

        # Sample candidates from W and M1 tails
        cand_w = self._sample_cold(cache_snapshot, self.W)
        cand_m1 = self._sample_cold(cache_snapshot, self.M1)

        # If both available, evict colder by comparator
        if cand_w is not None and cand_m1 is not None:
            ct_w = self._cold_tuple(cache_snapshot, cand_w)
            ct_m1 = self._cold_tuple(cache_snapshot, cand_m1)
            victim = cand_w if ct_w <= ct_m1 else cand_m1
            return victim

        # If only one of W/M1 exists, prefer it
        if cand_w is not None:
            return cand_w
        if cand_m1 is not None:
            return cand_m1

        # As a last resort, consider M2 with strong protection bias.
        if self.M2:
            # Bias M2 by +1 on frequency to protect it
            cand_m2 = self._sample_cold(cache_snapshot, self.M2, m2_bias=1 + (1 if in_cooldown else 0))
            if cand_m2 is not None:
                # Only allow evicting M2 if new is clearly hotter
                f_new = self._sketch_est(cache_snapshot, obj.key)
                f_m2 = self._sketch_est(cache_snapshot, cand_m2)
                if f_new >= f_m2 + (2 + (1 if in_cooldown else 0)):
                    return cand_m2
                # Otherwise, reluctantly evict absolute LRU among all present (fallback)
                # but try to avoid hot M2 by picking its LRU
                return cand_m2

        # Fallback: any key from cache
        for k in cache_snapshot.cache.keys():
            return k

    def update_after_hit(self, cache_snapshot, obj):
        self._reset_if_new_run(cache_snapshot)
        self._prune_metadata(cache_snapshot)
        self._ensure_sketch(cache_snapshot)

        now = cache_snapshot.access_count
        key = obj.key

        # Phase update: a hit
        self._update_phase(cache_snapshot, is_miss=False)

        # Learn frequency and recency
        self._sketch_add(cache_snapshot, key, 1)
        self._touch_score(key, now, add=1.0)

        in_cooldown = now < self.cooldown_until

        if key in self.M2:
            self._move_to_mru(self.M2, key)
        elif key in self.M1:
            # Promote to M2 if frequent and not in cooldown
            if not in_cooldown and self._sketch_est(cache_snapshot, key) >= 3:
                self.M1.pop(key, None)
                self._move_to_mru(self.M2, key)
            else:
                self._move_to_mru(self.M1, key)
        elif key in self.W:
            # Warm up: move to M1; promote further if clearly hot
            self.W.pop(key, None)
            if not in_cooldown and self._sketch_est(cache_snapshot, key) >= 3:
                self._move_to_mru(self.M2, key)
            else:
                self._move_to_mru(self.M1, key)
        else:
            # Metadata miss but cache hit: place conservatively into M1
            self._move_to_mru(self.M1, key)

        self._enforce_targets(cache_snapshot)
        self._trim_ghosts(cache_snapshot)

    def update_after_insert(self, cache_snapshot, obj):
        self._reset_if_new_run(cache_snapshot)
        self._prune_metadata(cache_snapshot)
        self._ensure_sketch(cache_snapshot)

        now = cache_snapshot.access_count
        key = obj.key

        # Phase update: a miss
        self._update_phase(cache_snapshot, is_miss=True)

        # Remove stale placements if any
        self.W.pop(key, None)
        self.M1.pop(key, None)
        self.M2.pop(key, None)

        # Learn on admission
        self._sketch_add(cache_snapshot, key, 1)
        # Start with a small score to reduce scan pollution
        self._touch_score(key, now, add=0.5)

        in_cooldown = now < self.cooldown_until

        # Ghost hits -> strong admission to protected
        if key in self.G1:
            self.G1.pop(key, None)
            # Admission to M2 to capture recurrence
            self._move_to_mru(self.M2, key)
        elif key in self.G2:
            self.G2.pop(key, None)
            self._move_to_mru(self.M2, key)
        else:
            # Fresh miss: scan-guard -> prefer W
            if in_cooldown:
                self._move_to_mru(self.W, key)
            else:
                f_new = self._sketch_est(cache_snapshot, key)
                self._admit_segment(cache_snapshot, key, f_new)

        # Enforce segment targets
        self._enforce_targets(cache_snapshot)
        self._trim_ghosts(cache_snapshot)

    def update_after_evict(self, cache_snapshot, obj, evicted_obj):
        self._reset_if_new_run(cache_snapshot)
        evk = evicted_obj.key

        from_seg = None
        if evk in self.W:
            self.W.pop(evk, None)
            from_seg = "W"
        elif evk in self.M1:
            self.M1.pop(evk, None)
            from_seg = "M1"
        elif evk in self.M2:
            self.M2.pop(evk, None)
            from_seg = "M2"

        # Place in appropriate ghost
        if from_seg in ("W", "M1"):
            self.G1[evk] = None
        elif from_seg == "M2":
            self.G2[evk] = None

        # Clean up recency metadata
        self.score.pop(evk, None)
        self.last_time.pop(evk, None)

        self._trim_ghosts(cache_snapshot)


# Singleton instance
_policy = _CSLRU_TinyLFU_Compete()


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