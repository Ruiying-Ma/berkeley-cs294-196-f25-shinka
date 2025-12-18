# EVOLVE-BLOCK-START
"""LeCaR (learning LRU/LFU mix) + TinyLFU guard

Public API:
- evict(cache_snapshot, obj) -> key
- update_after_hit(cache_snapshot, obj)
- update_after_insert(cache_snapshot, obj)
- update_after_evict(cache_snapshot, obj, evicted_obj)
"""

from collections import OrderedDict


class _LeCaR_TinyLFU:
    """
    LeCaR policy that mixes LRU and LFU using online multiplicative weights.
    - LRU: OrderedDict for recency.
    - LFU: frequency buckets (freq -> OrderedDict of keys) with min_freq tracking.
    - TinyLFU: Count-Min Sketch for capacity-aware frequency estimates and aging.
    - Learning: when a miss occurs on a key previously evicted by LRU (or LFU),
      penalize that policy and reward the other; choose eviction from the
      stronger policy, with TinyLFU-guarded tie-breaking.
    """

    __slots__ = (
        # LRU state
        "lru",
        # LFU state
        "key_freq", "freq_buckets", "min_freq",
        # TinyLFU sketch
        "SKETCH_DEPTH", "sketch_w", "sketch", "sketch_ops", "age_threshold",
        # Learning weights
        "w_lru", "w_lfu", "eta",
        # Evicted-by history for regret
        "evicted_by", "evicted_limit",
        # Bookkeeping
        "last_access_seen", "last_victim_key", "last_victim_policy",
        # Sampling
        "_sample_k",
    )

    def __init__(self):
        # Resident structures
        self.lru = OrderedDict()

        self.key_freq = {}                 # key -> freq
        self.freq_buckets = {}             # freq -> OrderedDict(keys)
        self.min_freq = 1

        # TinyLFU CMS
        self.SKETCH_DEPTH = 4
        self.sketch_w = 0
        self.sketch = []
        self.sketch_ops = 0
        self.age_threshold = 0

        # Learning weights
        self.w_lru = 1.0
        self.w_lfu = 1.0
        self.eta = 0.05

        # Evicted key -> policy that evicted it
        self.evicted_by = OrderedDict()
        self.evicted_limit = 0

        # Misc
        self.last_access_seen = -1
        self.last_victim_key = None
        self.last_victim_policy = None

        # Sampling size
        self._sample_k = 8

    # ---------- lifecycle ----------

    def _cap(self, cache_snapshot):
        return max(1, int(cache_snapshot.capacity))

    def _reset_if_new_run(self, cache_snapshot):
        if cache_snapshot.access_count <= 1 or self.last_access_seen > cache_snapshot.access_count:
            self.lru.clear()
            self.key_freq.clear()
            self.freq_buckets.clear()
            self.min_freq = 1

            self.sketch_w = 0
            self.sketch = []
            self.sketch_ops = 0
            self.age_threshold = 0

            self.w_lru = 1.0
            self.w_lfu = 1.0
            self.evicted_by.clear()
            self.evicted_limit = 0

            self.last_victim_key = None
            self.last_victim_policy = None
            self._sample_k = 8

        self.last_access_seen = cache_snapshot.access_count

    def _prune_metadata(self, cache_snapshot):
        # Keep LRU and LFU in sync with actual cache contents
        cache_keys = set(cache_snapshot.cache.keys())

        # LRU prune
        for k in list(self.lru.keys()):
            if k not in cache_keys:
                self.lru.pop(k, None)

        # LFU prune
        for k in list(self.key_freq.keys()):
            if k not in cache_keys:
                self._lfu_remove(k)

        # Bound evicted_by map
        cap = self._cap(cache_snapshot)
        if self.evicted_limit == 0:
            self.evicted_limit = 4 * cap
        while len(self.evicted_by) > self.evicted_limit:
            self.evicted_by.popitem(last=False)

    def _seed_from_cache(self, cache_snapshot):
        # Seed structures on first calls if cache already populated
        if not self.lru and cache_snapshot.cache:
            for k in cache_snapshot.cache.keys():
                self._lru_mru(k)
                self._lfu_add_new(k)

    # ---------- TinyLFU CMS ----------

    def _ensure_sketch(self, cache_snapshot):
        if self.sketch_w:
            return
        cap = self._cap(cache_snapshot)
        target = max(512, 4 * cap)  # capacity-aware width
        w = 1
        while w < target:
            w <<= 1
        self.sketch_w = w
        self.sketch = [[0] * self.sketch_w for _ in range(self.SKETCH_DEPTH)]
        self.sketch_ops = 0
        # Age approximately every [4C, 16C] updates
        self.age_threshold = max(4 * cap, min(16 * cap, 8 * cap))
        # Sampling scale to capacity
        self._sample_k = max(4, min(12, (cap // 8) or 4))
        # Bound evicted_by
        self.evicted_limit = 4 * cap

    def _hash_idx(self, key, i):
        return (hash((key, i, 0x9E3779B97F4A7C15)) & (self.sketch_w - 1))

    def _sketch_add(self, cache_snapshot, key, delta=1):
        self._ensure_sketch(cache_snapshot)
        if not self.sketch_w:
            return
        for i in range(self.SKETCH_DEPTH):
            self.sketch[i][self._hash_idx(key, i)] += delta
        self.sketch_ops += 1
        if self.sketch_ops >= self.age_threshold:
            for i in range(self.SKETCH_DEPTH):
                row = self.sketch[i]
                for j in range(self.sketch_w):
                    row[j] >>= 1
            self.sketch_ops = 0

    def _sketch_est(self, cache_snapshot, key):
        self._ensure_sketch(cache_snapshot)
        if not self.sketch_w:
            return 0
        est = None
        for i in range(self.SKETCH_DEPTH):
            v = self.sketch[i][self._hash_idx(key, i)]
            est = v if est is None or v < est else est
        return est if est is not None else 0

    # ---------- LRU helpers ----------

    def _lru_mru(self, key):
        if key in self.lru:
            self.lru.move_to_end(key, last=True)
        else:
            self.lru[key] = None

    def _lru_remove(self, key):
        self.lru.pop(key, None)

    def _sample_lru(self, cache_snapshot):
        # Sample up to K from LRU side and pick lowest TinyLFU estimate
        if not self.lru:
            return None
        k = min(self._sample_k, len(self.lru))
        it = iter(self.lru.keys())  # from LRU to MRU
        best_k, best_f = None, None
        for _ in range(k):
            cand = next(it)
            f = self._sketch_est(cache_snapshot, cand)
            if best_k is None or f < best_f:
                best_k, best_f = cand, f
                if best_f == 0:
                    break
        return best_k if best_k is not None else next(iter(self.lru))

    # ---------- LFU helpers ----------

    def _bucket(self, freq):
        b = self.freq_buckets.get(freq)
        if b is None:
            b = OrderedDict()
            self.freq_buckets[freq] = b
        return b

    def _lfu_add_new(self, key):
        # New items start at freq=1
        self.key_freq[key] = 1
        self._bucket(1)[key] = None
        self.min_freq = 1

    def _lfu_inc(self, key):
        f = self.key_freq.get(key)
        if f is None:
            # If not found (metadata miss), add new
            self._lfu_add_new(key)
            return
        b = self._bucket(f)
        b.pop(key, None)
        if not b:
            # Remove empty bucket
            self.freq_buckets.pop(f, None)
            if self.min_freq == f:
                self.min_freq = f + 1
        nf = f + 1
        self.key_freq[key] = nf
        self._bucket(nf)[key] = None

    def _lfu_remove(self, key):
        f = self.key_freq.pop(key, None)
        if f is None:
            return
        b = self._bucket(f)
        b.pop(key, None)
        if not b:
            self.freq_buckets.pop(f, None)
            if self.min_freq == f:
                # Recompute min_freq
                if self.key_freq:
                    self.min_freq = min(self.key_freq.values())
                else:
                    self.min_freq = 1

    def _lfu_victim_bucket(self):
        # Return the coldest non-empty bucket and its frequency
        if not self.key_freq:
            return None, None
        f = self.min_freq
        if f in self.freq_buckets and self.freq_buckets[f]:
            return self.freq_buckets[f], f
        # Fallback: find the smallest available freq
        if self.freq_buckets:
            f = min(self.freq_buckets.keys())
            self.min_freq = f
            return self.freq_buckets[f], f
        return None, None

    def _sample_lfu(self, cache_snapshot):
        b, _ = self._lfu_victim_bucket()
        if not b:
            return None
        k = min(self._sample_k, len(b))
        it = iter(b.keys())  # LRU within bucket
        best_k, best_f = None, None
        for _ in range(k):
            cand = next(it)
            f = self._sketch_est(cache_snapshot, cand)
            if best_k is None or f < best_f:
                best_k, best_f = cand, f
                if best_f == 0:
                    break
        # Fallback to LRU in bucket
        return best_k if best_k is not None else next(iter(b))

    # ---------- learning ----------

    def _normalize_weights(self):
        s = self.w_lru + self.w_lfu
        if s <= 0:
            self.w_lru = self.w_lfu = 1.0
            s = 2.0
        # Normalize to sum=2 for stability
        self.w_lru = 2.0 * (self.w_lru / s)
        self.w_lfu = 2.0 * (self.w_lfu / s)

    def _choose_policy(self):
        # Choose the stronger policy deterministically (argmax)
        return "LRU" if self.w_lru >= self.w_lfu else "LFU"

    def _reward_policy(self, good, bad):
        # Multiplicative weights: reward good, penalize bad
        if good == "LRU":
            self.w_lru *= (1.0 + self.eta)
            self.w_lfu *= (1.0 - self.eta)
        else:
            self.w_lfu *= (1.0 + self.eta)
            self.w_lru *= (1.0 - self.eta)
        # Clamp to keep positive
        self.w_lru = max(self.w_lru, 1e-6)
        self.w_lfu = max(self.w_lfu, 1e-6)
        self._normalize_weights()

    # ---------- public API ----------

    def evict(self, cache_snapshot, obj):
        self._reset_if_new_run(cache_snapshot)
        self._prune_metadata(cache_snapshot)
        self._ensure_sketch(cache_snapshot)
        self._seed_from_cache(cache_snapshot)

        cand_lru = self._sample_lru(cache_snapshot)
        cand_lfu = self._sample_lfu(cache_snapshot)

        # If one structure is empty, fall back to the other
        if cand_lru is None and cand_lfu is None:
            # Fallback: any key
            for k in cache_snapshot.cache.keys():
                self.last_victim_key = k
                self.last_victim_policy = "LRU" if k in self.lru else ("LFU" if k in self.key_freq else None)
                return k
            return None
        if cand_lru is None:
            chosen = cand_lfu
            chosen_policy = "LFU"
        elif cand_lfu is None:
            chosen = cand_lru
            chosen_policy = "LRU"
        else:
            # Choose policy by learned weights
            policy = self._choose_policy()
            chosen = cand_lru if policy == "LRU" else cand_lfu
            chosen_policy = policy

            # TinyLFU guard: avoid evicting a much hotter item than the incoming object
            f_new = self._sketch_est(cache_snapshot, obj.key)
            f_ch = self._sketch_est(cache_snapshot, chosen)
            other = cand_lfu if chosen_policy == "LRU" else cand_lru
            f_ot = self._sketch_est(cache_snapshot, other) if other is not None else None
            # If chosen is significantly hotter than incoming and the other is not worse, switch
            if f_ch > f_new + 1 and (f_ot is not None and f_ot <= f_ch):
                chosen = other
                chosen_policy = "LFU" if chosen_policy == "LRU" else "LRU"

        self.last_victim_key = chosen
        self.last_victim_policy = chosen_policy
        return chosen

    def update_after_hit(self, cache_snapshot, obj):
        self._reset_if_new_run(cache_snapshot)
        self._prune_metadata(cache_snapshot)
        self._ensure_sketch(cache_snapshot)

        k = obj.key
        # Learn in TinyLFU
        self._sketch_add(cache_snapshot, k, 1)

        # Update LRU recency
        self._lru_mru(k)

        # Update LFU frequency
        if k in self.key_freq:
            self._lfu_inc(k)
        else:
            # If metadata desynced (e.g., seeded late), add new
            self._lfu_add_new(k)

    def update_after_insert(self, cache_snapshot, obj):
        self._reset_if_new_run(cache_snapshot)
        self._prune_metadata(cache_snapshot)
        self._ensure_sketch(cache_snapshot)

        k = obj.key
        # Learn in TinyLFU on admission
        self._sketch_add(cache_snapshot, k, 1)

        # If this key was previously evicted, update regret
        policy = self.evicted_by.pop(k, None)
        if policy == "LRU":
            # LRU evicted it and we missed again -> reward LFU
            self._reward_policy(good="LFU", bad="LRU")
        elif policy == "LFU":
            self._reward_policy(good="LRU", bad="LFU")

        # Insert into resident structures
        self._lru_mru(k)
        if k in self.key_freq:
            # If reinserted while metadata remained, treat as access
            self._lfu_inc(k)
        else:
            self._lfu_add_new(k)

    def update_after_evict(self, cache_snapshot, obj, evicted_obj):
        self._reset_if_new_run(cache_snapshot)
        evk = evicted_obj.key

        # Remove from resident structures
        self._lru_remove(evk)
        self._lfu_remove(evk)

        # Record in evicted-by map for regret on future re-reference
        policy = self.last_victim_policy
        if policy is None:
            # Heuristic fallback if not recorded
            policy = "LRU" if evk in self.lru else ("LFU" if evk in self.key_freq else "LRU")
        self.evicted_by[evk] = policy

        # Bound evicted_by size
        cap = self._cap(cache_snapshot)
        self.evicted_limit = max(self.evicted_limit, 4 * cap)
        while len(self.evicted_by) > self.evicted_limit:
            self.evicted_by.popitem(last=False)

        # Clear last victim marker if matches
        if self.last_victim_key == evk:
            self.last_victim_key = None
            self.last_victim_policy = None


# Singleton policy instance
_policy = _LeCaR_TinyLFU()


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