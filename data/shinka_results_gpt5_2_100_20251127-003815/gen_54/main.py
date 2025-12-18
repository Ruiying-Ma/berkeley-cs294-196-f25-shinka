# EVOLVE-BLOCK-START
"""LeCaR-inspired adaptive LRU/LFU eviction with ghost-based online learning.

Public API:
- evict(cache_snapshot, obj) -> key
- update_after_hit(cache_snapshot, obj)
- update_after_insert(cache_snapshot, obj)
- update_after_evict(cache_snapshot, obj, evicted_obj)
"""

import math
from collections import OrderedDict


class _LeCaRPolicy:
    """
    LeCaR-style adaptive replacement policy:
    - Maintains an LRU recency order for the live cache keys.
    - Maintains simple per-key frequency counters (aged periodically).
    - Keeps two ghost caches: G_LRU stores keys last evicted by LRU, G_LFU by LFU.
    - Learns weights for experts (w_lru, w_lfu) using multiplicative updates:
        * If an accessed key appears in G_LRU => penalize LRU (regret for its eviction).
        * If an accessed key appears in G_LFU => penalize LFU.
      Normalize the weights after each update.
    - Eviction decision is taken by the expert with higher current weight:
        * LRU victim: the LRU key.
        * LFU victim: min-frequency key among a small sample from the LRU tail.
    """

    __slots__ = (
        "recency",        # OrderedDict for live keys: LRU -> MRU
        "freq",           # dict: key -> int frequency
        "ghost_lru",      # OrderedDict ghost for keys evicted by LRU
        "ghost_lfu",      # OrderedDict ghost for keys evicted by LFU
        "w_lru", "w_lfu", # expert weights
        "eta",            # learning rate
        "capacity",       # last seen capacity
        "last_time",      # last seen access_count
        "age_ops", "age_period",
        "sample_k",       # sampling width from LRU tail when doing LFU victim selection
        "last_choice"     # last eviction policy chosen: "LRU" or "LFU" or None
    )

    def __init__(self):
        self.recency = OrderedDict()
        self.freq = {}
        self.ghost_lru = OrderedDict()
        self.ghost_lfu = OrderedDict()
        self.w_lru = 0.5
        self.w_lfu = 0.5
        self.eta = 0.12
        self.capacity = None
        self.last_time = -1
        self.age_ops = 0
        self.age_period = 1024
        self.sample_k = 6
        self.last_choice = None

    # ------------ internals ------------

    def _reset_if_new_run(self, cache_snapshot):
        acc = cache_snapshot.access_count
        cap = max(int(cache_snapshot.capacity), 1)
        if self.capacity is None:
            self.capacity = cap
            self.last_time = acc
            self.age_period = max(512, cap * 8)
            self.sample_k = max(4, min(12, (cap // 8) or 4))
            return
        # New run or counter reset
        if acc <= 1 or acc < self.last_time or self.capacity != cap:
            self.recency.clear()
            self.freq.clear()
            self.ghost_lru.clear()
            self.ghost_lfu.clear()
            self.w_lru, self.w_lfu = 0.5, 0.5
            self.last_choice = None
            self.capacity = cap
            self.age_ops = 0
            self.age_period = max(512, cap * 8)
            self.sample_k = max(4, min(12, (cap // 8) or 4))
        self.last_time = acc

    def _self_heal(self, cache_snapshot):
        # Ensure recency contains exactly the live cache keys (order preserved where possible).
        live = cache_snapshot.cache.keys()
        # Remove keys not present
        for k in list(self.recency.keys()):
            if k not in cache_snapshot.cache:
                self.recency.pop(k, None)
        # Add missing keys (append as MRU)
        for k in live:
            if k not in self.recency:
                self.recency[k] = None
        # Keep freq map bounded
        for k in list(self.freq.keys()):
            if k not in cache_snapshot.cache and k not in self.ghost_lru and k not in self.ghost_lfu:
                # keep some freq for ghosts as well
                self.freq.pop(k, None)

    def _age_freq_if_needed(self):
        self.age_ops += 1
        if self.age_ops % max(1, self.age_period) == 0:
            for k in list(self.freq.keys()):
                v = self.freq.get(k, 0) >> 1
                if v <= 0:
                    self.freq.pop(k, None)
                else:
                    self.freq[k] = v

    def _update_weights_on_access(self, key: str):
        # If key in a ghost list, penalize the corresponding expert (regret).
        loss_lru = 1.0 if key in self.ghost_lru else 0.0
        loss_lfu = 1.0 if key in self.ghost_lfu else 0.0
        if loss_lru == 0.0 and loss_lfu == 0.0:
            return
        # Multiplicative weight update and renormalize
        wl = self.w_lru * math.exp(-self.eta * loss_lru)
        wf = self.w_lfu * math.exp(-self.eta * loss_lfu)
        s = wl + wf
        if s <= 0:
            wl, wf = 0.5, 0.5
        else:
            wl, wf = wl / s, wf / s
        # Add a tiny floor to avoid freezing
        eps = 1e-6
        wl = max(eps, min(1.0 - eps, wl))
        wf = max(eps, min(1.0 - eps, wf))
        # Renormalize after clamping
        s2 = wl + wf
        self.w_lru, self.w_lfu = wl / s2, wf / s2
        # Since key just caused regret, remove it from ghosts (avoid repeat penalties)
        self.ghost_lru.pop(key, None)
        self.ghost_lfu.pop(key, None)

    def _touch(self, key: str):
        # Move key to MRU in LRU structure
        self.recency[key] = None
        self.recency.move_to_end(key, last=True)

    def _lru_key(self):
        return next(iter(self.recency)) if self.recency else None

    def _sample_lfu_from_tail(self):
        # Sample up to sample_k keys from the LRU tail (oldest items) and choose min-frequency
        if not self.recency:
            return None
        k = min(self.sample_k, len(self.recency))
        it = iter(self.recency.keys())  # LRU -> MRU
        best_k = None
        best_f = None
        for _ in range(k):
            key = next(it)
            f = self.freq.get(key, 0)
            if best_f is None or f < best_f:
                best_f = f
                best_k = key
        return best_k if best_k is not None else self._lru_key()

    def _bound_ghosts(self):
        cap = max(int(self.capacity or 1), 1)
        while len(self.ghost_lru) > cap:
            self.ghost_lru.popitem(last=False)
        while len(self.ghost_lfu) > cap:
            self.ghost_lfu.popitem(last=False)

    # ------------ API hooks ------------

    def choose_victim(self, cache_snapshot, new_obj) -> str:
        self._reset_if_new_run(cache_snapshot)
        self._self_heal(cache_snapshot)

        # Expert choice: deterministically pick the higher-weight policy
        policy = "LRU" if self.w_lru >= self.w_lfu else "LFU"

        # Candidates
        if policy == "LRU":
            victim = self._lru_key()
            if victim is None:
                victim = self._sample_lfu_from_tail()
        else:
            victim = self._sample_lfu_from_tail()
            if victim is None:
                victim = self._lru_key()

        # Safety fallback
        if victim is None:
            victim = next(iter(cache_snapshot.cache))

        self.last_choice = policy
        return victim

    def on_hit(self, cache_snapshot, obj):
        self._reset_if_new_run(cache_snapshot)
        self._self_heal(cache_snapshot)
        self._age_freq_if_needed()

        k = obj.key
        # Update learning based on ghosts (regret)
        self._update_weights_on_access(k)

        # Frequency bump and LRU touch
        self.freq[k] = self.freq.get(k, 0) + 1
        self._touch(k)

    def on_insert(self, cache_snapshot, obj):
        self._reset_if_new_run(cache_snapshot)
        self._self_heal(cache_snapshot)
        self._age_freq_if_needed()

        k = obj.key
        # Update learning based on ghosts (regret)
        self._update_weights_on_access(k)

        # Initialize or bump a little to favor quick reuses
        self.freq[k] = self.freq.get(k, 0) + 1
        self._touch(k)

    def on_evict(self, cache_snapshot, obj, evicted_obj):
        self._reset_if_new_run(cache_snapshot)
        # Remove from live structures
        ek = evicted_obj.key
        self.recency.pop(ek, None)
        # Keep freq for ghosts for a while (useful for LFU regret), but can also prune old ones
        # Place into corresponding ghost according to last chosen policy
        if self.last_choice == "LRU":
            self.ghost_lru.pop(ek, None)
            self.ghost_lru[ek] = None  # MRU of ghost LRU
        elif self.last_choice == "LFU":
            self.ghost_lfu.pop(ek, None)
            self.ghost_lfu[ek] = None  # MRU of ghost LFU
        # Bound ghost sizes
        self._bound_ghosts()
        # Reset last choice
        self.last_choice = None


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