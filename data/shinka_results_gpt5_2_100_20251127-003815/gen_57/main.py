# EVOLVE-BLOCK-START
"""Hybrid W-TinyLFU + LRFU-decayed scoring with SLRU main segments.

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
    Windowed TinyLFU + SLRU main with LRFU-style decayed scores:
    - W: window LRU (recency buffer).
    - M1: main probationary (first-time in main).
    - M2: main protected (promoted on re-use).
    - TinyLFU sketch for admission decisions.
    - LRFU decayed scores for intra-segment victim selection and demotion.
    """

    __slots__ = (
        "W", "M1", "M2", "capacity",
        "win_frac", "prot_frac", "sketch", "_sample_k",
        "hits_w", "hits_main", "last_tune_time", "tune_period",
        "score", "last_time", "decay_base", "decay_half_life"
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
        # LRFU decayed score state
        self.score = {}     # key -> float decayed score
        self.last_time = {} # key -> last access_count
        self.decay_half_life = 16
        self.decay_base = 2 ** (-1.0 / self.decay_half_life)

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
            # LRFU decay tuned to capacity: shorter half-life for small caches
            self.decay_half_life = max(8, min(64, (self.capacity // 2) or 8))
            self.decay_base = 2 ** (-1.0 / float(self.decay_half_life))
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
            self.decay_half_life = max(8, min(64, (self.capacity // 2) or 8))
            self.decay_base = 2 ** (-1.0 / float(self.decay_half_life))

    def _targets(self):
        cap = self.capacity or 1
        w_tgt = max(1, int(round(cap * self.win_frac)))
        main_cap = max(0, cap - w_tgt)
        prot_tgt = int(round(main_cap * self.prot_frac))
        prob_tgt = max(0, main_cap - prot_tgt)
        return w_tgt, prob_tgt, prot_tgt

    def _ensure_meta(self, k: str, now: int):
        if k not in self.last_time:
            self.last_time[k] = now
        if k not in self.score:
            self.score[k] = 0.0

    def _decayed_score(self, k: str, now: int) -> float:
        # Lazily decay the score to 'now'
        self._ensure_meta(k, now)
        old = self.last_time[k]
        dt = now - old
        if dt > 0:
            self.score[k] *= self.decay_base ** dt
            self.last_time[k] = now
        return self.score[k]

    def _self_heal(self, cache_snapshot):
        # Ensure all cached keys are tracked and no phantom entries remain.
        now = cache_snapshot.access_count
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
                self._ensure_meta(k, now)

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

    def _sample_cold_candidate(self, od: OrderedDict, now: int):
        """
        Return (key, tiny_est, decayed) for the coldest among a randomized
        slice of the LRU tail. We consider up to 4K oldest entries and sample
        K contiguous keys from a pseudo-random offset to avoid deterministic
        tail bias. Choose lexicographic min on (tiny_est, decayed_score).
        """
        if not od:
            return None, None, None
        # Determine sampling window within the LRU tail
        k = min(self._sample_k, len(od))
        tail_len = min(len(od), k * 4)
        # Collect the tail keys (LRU->MRU up to tail_len)
        tail_keys = []
        it = iter(od.keys())
        for _ in range(tail_len):
            try:
                tail_keys.append(next(it))
            except StopIteration:
                break
        if not tail_keys:
            return None, None, None
        # Pseudo-random offset based on time to diversify selection
        span = max(1, tail_len - k + 1)
        off = (now * 1103515245 + 12345) & 0x7FFFFFFF
        off %= span
        # Evaluate candidates in the sampled window
        best_k, best_est, best_dec = None, None, None
        for idx in range(off, off + k):
            key = tail_keys[idx]
            est = self.sketch.estimate(key)
            dec = self._decayed_score(key, now)
            if (best_est is None
                or est < best_est
                or (est == best_est and dec < best_dec)):
                best_k, best_est, best_dec = key, est, dec
                if best_est == 0 and best_dec == 0.0:
                    break
        return best_k, best_est, best_dec

    # ----- policy decisions -----

    def choose_victim(self, cache_snapshot, new_obj) -> str:
        """
        Hybrid eviction with guarded admission:
        - Prefer evicting a cold M1 entry if the incoming item is hotter.
        - Else evict from window W (to protect main).
        - Only consider evicting from M2 when it is oversized or W is empty,
          and only if the incoming item is clearly hotter than the cold M2 candidate.
        - Fall back to the colder of M1/M2 with a protection bias.
        """
        self._ensure_capacity(cache_snapshot.capacity)
        self._self_heal(cache_snapshot)

        now = cache_snapshot.access_count
        _, _, prot_tgt = self._targets()

        # Candidates
        cand_w = self._lru(self.W)
        cand_m1, f_m1, d_m1 = self._sample_cold_candidate(self.M1, now)
        cand_m2, f_m2, d_m2 = self._sample_cold_candidate(self.M2, now)

        f_new = self.sketch.estimate(new_obj.key)

        # Admission gate against M1: if new is hotter than a cold M1, replace it
        if cand_m1 is not None and f_new >= (f_m1 or 0) + 1:
            return cand_m1

        # Guarded check against M2: only when M2 oversized or no window to shed
        if cand_m2 is not None:
            if len(self.M2) > prot_tgt or cand_w is None:
                if f_new >= (f_m2 or 0) + 2:
                    return cand_m2

        # Otherwise, evict from the window to preserve main
        if cand_w is not None:
            return cand_w

        # No window; choose between M1 and M2 with protective bias
        if cand_m1 is None and cand_m2 is None:
            # Last resort: any key from cache
            return next(iter(cache_snapshot.cache))
        if cand_m1 is None:
            return cand_m2
        if cand_m2 is None:
            return cand_m1

        # Compute adjusted scores (lower is colder); bias protects M2
        score_m1 = (f_m1 or 0)
        score_m2 = (f_m2 or 0) + 1
        if score_m1 < score_m2:
            return cand_m1
        if score_m2 < score_m1:
            return cand_m2
        # Tie-break with decayed score (LRFU)
        if (d_m1 or 0.0) <= (d_m2 or 0.0):
            return cand_m1
        return cand_m2

    def on_hit(self, cache_snapshot, obj):
        """
        Hit processing:
        - Increment TinyLFU and LRFU-decayed score.
        - W hit: refresh or early promote if sufficiently hot.
        - M1 hit: promote to M2.
        - M2 hit: refresh in M2.
        - If untracked but hit (desync): treat as warm and place into M2.
        """
        self._ensure_capacity(cache_snapshot.capacity)
        now = cache_snapshot.access_count
        key = obj.key

        # Update TinyLFU and LRFU score
        self.sketch.increment(key, 1)
        s = self._decayed_score(key, now)
        self.score[key] = s + 1.0

        if key in self.W:
            self.hits_w += 1
            # Early promotion if strong frequency to avoid window churn
            est = self.sketch.estimate(key)
            dec = self._decayed_score(key, now)
            _, _, prot_tgt = self._targets()
            promote = False
            if len(self.M2) < prot_tgt:
                # Underfilled protected: promote more aggressively
                if est >= 2 or dec >= 1.0:
                    promote = True
            else:
                # Normal thresholds
                if est >= 3 or dec >= 1.5:
                    promote = True
            if promote:
                # Move from window to protected
                self.W.pop(key, None)
                self._touch(self.M2, key)
                # Keep protected region within target using decayed-aware demotion
                if len(self.M2) > prot_tgt:
                    demote, _, _ = self._sample_cold_candidate(self.M2, now)
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
            # Rebalance protected size if needed (decayed-aware demotion)
            _, _, prot_tgt = self._targets()
            if len(self.M2) > prot_tgt:
                demote, _, _ = self._sample_cold_candidate(self.M2, now)
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
            demote, _, _ = self._sample_cold_candidate(self.M2, now)
            if demote is not None:
                self.M2.pop(demote, None)
                self._touch(self.M1, demote)
        self._maybe_tune(now)

    def on_insert(self, cache_snapshot, obj):
        """
        Insert (on miss) processing:
        - Initialize LRFU metadata modestly (to reduce scan pollution).
        - Increment TinyLFU.
        - Insert new key into window W (MRU) or directly into protected if clearly hot.
        - If W exceeds target, move W's LRU to main probationary (M1).
        - Keep protected region within target by demoting a decayed-cold entry if needed.
        """
        self._ensure_capacity(cache_snapshot.capacity)
        now = cache_snapshot.access_count
        key = obj.key
        self.sketch.increment(key, 1)

        # Initialize decayed metadata
        self.last_time[key] = now
        self.score[key] = 0.5

        # Ensure it's not tracked elsewhere
        self.W.pop(key, None)
        self.M1.pop(key, None)
        self.M2.pop(key, None)

        # Decide placement: allow early protected admission for clearly hot keys
        w_tgt, _, prot_tgt = self._targets()
        est = self.sketch.estimate(key)
        if est >= 5 or (len(self.M2) < prot_tgt and est >= 3):
            # Early promotion to protected to avoid window churn
            self._touch(self.M2, key)
        else:
            # Insert into window
            self._touch(self.W, key)

        # Rebalance: if W is beyond target, move W's LRU to M1 (admission path)
        if len(self.W) > w_tgt:
            w_lru = self._lru(self.W)
            if w_lru is not None and w_lru != key:
                self.W.pop(w_lru, None)
                # Move into M1 probationary
                self._touch(self.M1, w_lru)

        # Keep protected region within target (decayed-aware demotion)
        if len(self.M2) > prot_tgt:
            demote, _, _ = self._sample_cold_candidate(self.M2, now)
            if demote is not None:
                self.M2.pop(demote, None)
                self._touch(self.M1, demote)

        # Periodically tune window size
        self._maybe_tune(now)

    def on_evict(self, cache_snapshot, obj, evicted_obj):
        """
        Eviction post-processing:
        - Remove evicted key from whichever segment it resides in and purge LRFU meta.
        """
        self._ensure_capacity(cache_snapshot.capacity)
        k = evicted_obj.key
        self.W.pop(k, None)
        self.M1.pop(k, None)
        self.M2.pop(k, None)
        self.score.pop(k, None)
        self.last_time.pop(k, None)


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