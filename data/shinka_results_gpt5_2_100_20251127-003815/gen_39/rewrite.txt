# EVOLVE-BLOCK-START
"""LeCaR-blend: Online-learning eviction by blending LRU and LFU experts.

Public API:
- evict(cache_snapshot, obj) -> key
- update_after_hit(cache_snapshot, obj)
- update_after_insert(cache_snapshot, obj)
- update_after_evict(cache_snapshot, obj, evicted_obj)
"""

from collections import OrderedDict
import math


class _LeCaRPolicy:
    """
    LeCaR-style blend of LRU and LFU:
    - R: resident recency (tracks current cache keys and their LRU order)
    - freq: lightweight global frequency with periodic aging
    - shadow_LRU: key-only LRU shadow cache of size = capacity (simulated pure LRU)
    - shadow_LFU: key-only LFU-ish shadow cache (size = capacity, pick low-freq victims)
    - weights: multiplicative weights for experts [LRU, LFU]
    - epsilon: exploration; eta: learning rate
    - sample_k: number of keys sampled from LRU tail when selecting LFU victims (fast approx)
    """

    __slots__ = (
        "cap", "R", "freq", "ops", "age_period",
        "shadow_LRU", "shadow_LFU",
        "w_lru", "w_lfu", "epsilon", "eta",
        "sample_k", "rng_salt", "_last_seen_access"
    )

    def __init__(self):
        self.cap = 1
        self.R = OrderedDict()
        self.freq = {}
        self.ops = 0
        self.age_period = 1024
        self.shadow_LRU = OrderedDict()
        self.shadow_LFU = OrderedDict()
        self.w_lru = 1.0
        self.w_lfu = 1.0
        self.epsilon = 0.05
        self.eta = 0.05
        self.sample_k = 6
        self.rng_salt = 0x9e3779b97f4a7c15
        self._last_seen_access = -1

    # --------- utilities ---------

    def _ensure_capacity(self, cap: int, access_count: int):
        cap = max(int(cap), 1)
        # Reset on first use or capacity change
        if self.cap != cap:
            self.cap = cap
            self.R.clear()
            self.shadow_LRU.clear()
            self.shadow_LFU.clear()
            self.w_lru = 1.0
            self.w_lfu = 1.0
            self.sample_k = max(4, min(12, (cap // 8) or 4))
            # Age faster for small caches, slower for large
            self.age_period = max(512, min(16384, cap * 8))
            self.freq.clear()
            self.ops = 0
        # Reset on new run (time restarted)
        if access_count <= 1 or self._last_seen_access > access_count:
            self.R.clear()
            self.shadow_LRU.clear()
            self.shadow_LFU.clear()
            self.w_lru = 1.0
            self.w_lfu = 1.0
            self.freq.clear()
            self.ops = 0
        self._last_seen_access = access_count

    def _self_heal(self, cache_snapshot):
        # Keep resident set in sync with actual cache keys.
        cache_keys = set(cache_snapshot.cache.keys())
        # Remove keys no longer present
        for k in list(self.R.keys()):
            if k not in cache_keys:
                self.R.pop(k, None)
        # Add missing keys (place as MRU)
        for k in cache_keys:
            if k not in self.R:
                self.R[k] = None
                self.R.move_to_end(k, last=True)

    def _decay_freq_if_needed(self):
        self.ops += 1
        if self.ops % self.age_period == 0:
            # Halve all frequencies; drop zeros
            for k in list(self.freq.keys()):
                v = self.freq[k] >> 1
                if v <= 0:
                    self.freq.pop(k, None)
                else:
                    self.freq[k] = v

    def _probabilities(self):
        # Normalize weights, add epsilon exploration
        s = self.w_lru + self.w_lfu
        if s <= 0:
            plru = plfu = 0.5
        else:
            plru = (1.0 - self.epsilon) * (self.w_lru / s) + self.epsilon * 0.5
            plfu = (1.0 - self.epsilon) * (self.w_lfu / s) + self.epsilon * 0.5
        return plru, plfu

    def _rand01(self, key: str, t: int):
        # Deterministic pseudo-random in [0,1) using key and time
        h = hash(key) ^ (t * 0x9e3779b1) ^ self.rng_salt
        # Map to positive integer and scale
        x = (h & 0xFFFFFFFFFFFFFFF) / float(0x1FFFFFFFFFFFFFF)
        return x

    def _touch_lru(self, od: OrderedDict, key: str):
        od[key] = None
        od.move_to_end(key, last=True)

    def _shadow_lru_access(self, key: str):
        self._touch_lru(self.shadow_LRU, key)
        # Enforce shadow capacity
        while len(self.shadow_LRU) > self.cap:
            self.shadow_LRU.popitem(last=False)

    def _shadow_lfu_access(self, key: str):
        # Maintain membership and recency order for tie-breakers
        self._touch_lru(self.shadow_LFU, key)
        # Enforce shadow capacity by evicting lowest-frequency among a small sample near LRU
        while len(self.shadow_LFU) > self.cap:
            victim = self._minfreq_sample(self.shadow_LFU)
            if victim is None:
                self.shadow_LFU.popitem(last=False)
            else:
                self.shadow_LFU.pop(victim, None)

    def _minfreq_sample(self, od: OrderedDict):
        if not od:
            return None
        k = min(self.sample_k, len(od))
        it = iter(od.keys())  # LRU -> MRU
        best_k = None
        best_f = None
        for _ in range(k):
            key = next(it)
            f = self.freq.get(key, 0)
            if best_f is None or f < best_f:
                best_f = f
                best_k = key
        return best_k if best_k is not None else next(iter(od))

    def _victim_lru(self):
        return next(iter(self.R)) if self.R else None

    def _victim_lfu(self):
        # Approximate LFU: choose min frequency among a small LRU-side sample
        return self._minfreq_sample(self.R)

    # --------- public policy hooks ---------

    def choose_victim(self, cache_snapshot, new_obj) -> str:
        self._ensure_capacity(cache_snapshot.capacity, cache_snapshot.access_count)
        self._self_heal(cache_snapshot)

        # If our metadata is empty, fall back to any key
        if not self.R:
            return next(iter(cache_snapshot.cache))

        plru, plfu = self._probabilities()
        r = self._rand01(new_obj.key, cache_snapshot.access_count)
        if r < plru:
            cand = self._victim_lru()
        else:
            cand = self._victim_lfu()
        if cand is None:
            # Robust fallbacks
            cand = self._victim_lru() or self._victim_lfu() or next(iter(cache_snapshot.cache))
        return cand

    def on_hit(self, cache_snapshot, obj):
        self._ensure_capacity(cache_snapshot.capacity, cache_snapshot.access_count)
        self._self_heal(cache_snapshot)
        self._decay_freq_if_needed()

        k = obj.key
        # Frequency and recency update
        self.freq[k] = self.freq.get(k, 0) + 1
        self._touch_lru(self.R, k)

        # Shadow caches see every access
        self._shadow_lru_access(k)
        self._shadow_lfu_access(k)

    def on_insert(self, cache_snapshot, obj):
        self._ensure_capacity(cache_snapshot.capacity, cache_snapshot.access_count)
        self._self_heal(cache_snapshot)
        self._decay_freq_if_needed()

        k = obj.key

        # Reward experts that would have avoided this miss
        in_lru = (k in self.shadow_LRU)
        in_lfu = (k in self.shadow_LFU)
        plru, plfu = self._probabilities()

        # Assign rewards: credit the expert(s) whose shadow contains the key
        r_lru = 1.0 if (in_lru and not in_lfu) else (0.5 if (in_lru and in_lfu) else 0.0)
        r_lfu = 1.0 if (in_lfu and not in_lru) else (0.5 if (in_lru and in_lfu) else 0.0)

        # Multiplicative weight update (EXP/Hedge-style); inverse-probability correction
        if r_lru > 0 and plru > 0:
            self.w_lru *= math.exp(self.eta * (r_lru / plru))
        if r_lfu > 0 and plfu > 0:
            self.w_lfu *= math.exp(self.eta * (r_lfu / plfu))

        # Normalize weights to avoid drift
        s = self.w_lru + self.w_lfu
        if s > 0:
            self.w_lru /= s
            self.w_lfu /= s
        else:
            self.w_lru, self.w_lfu = 0.5, 0.5

        # Insert into resident metadata and update frequency
        self.freq[k] = self.freq.get(k, 0) + 1
        self._touch_lru(self.R, k)

        # Update shadows for this access
        self._shadow_lru_access(k)
        self._shadow_lfu_access(k)

    def on_evict(self, cache_snapshot, obj, evicted_obj):
        self._ensure_capacity(cache_snapshot.capacity, cache_snapshot.access_count)
        # Remove evicted key from resident set (shadows are independent)
        ek = evicted_obj.key
        self.R.pop(ek, None)
        # Do not manipulate shadows here; they evolve purely from accesses


# Single policy instance reused across calls
_policy = _LeCaRPolicy()


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