# EVOLVE-BLOCK-START
"""LeCaR-style adaptive mixture of LRU and LFU with ghost histories.

Public API:
- evict(cache_snapshot, obj) -> key
- update_after_hit(cache_snapshot, obj)
- update_after_insert(cache_snapshot, obj)
- update_after_evict(cache_snapshot, obj, evicted_obj)
"""

from collections import OrderedDict

class _LeCaRPolicy:
    """
    Adaptive mixture of LRU and LFU:
      - LRU: OrderedDict for recency.
      - LFU: O(1) buckets {freq -> OrderedDict}, key->freq map, and min_freq pointer.
      - Ghosts: ghost_lru, ghost_lfu (OrderedDicts) to learn from past evictions.
      - Weights: wlru, wlfu updated via multiplicative weights on ghost hits.

    Eviction:
      - Candidate from LRU: LRU tail.
      - Candidate from LFU: LRU within the min-frequency bucket.
      - Choose policy by comparing weights (bias toward LRU on scan-like miss streaks).
    """

    __slots__ = (
        "capacity", "lru",
        "lfu_freq", "lfu_buckets", "lfu_min_freq",
        "ghost_lru", "ghost_lfu",
        "wlru", "wlfu", "lr", "weight_floor",
        "miss_streak", "last_access_seen",
        "last_evict_policy"
    )

    def __init__(self):
        self.capacity = None
        # LRU structures
        self.lru = OrderedDict()
        # LFU structures
        self.lfu_freq = {}                 # key -> freq
        self.lfu_buckets = {}              # freq -> OrderedDict of keys
        self.lfu_min_freq = 1
        # Ghost caches
        self.ghost_lru = OrderedDict()
        self.ghost_lfu = OrderedDict()
        # LeCaR weights
        self.wlru = 1.0
        self.wlfu = 1.0
        self.lr = 0.05
        self.weight_floor = 1e-4
        # Scan detection
        self.miss_streak = 0
        # For run resets
        self.last_access_seen = -1
        # Track which policy performed the last eviction
        self.last_evict_policy = "LRU"

    # ---------- helpers ----------

    def _ensure_capacity(self, cap: int):
        cap = max(int(cap), 1)
        if self.capacity is None or self.capacity != cap:
            # Hard reset on capacity change or first run
            self.capacity = cap
            self.lru.clear()
            self.lfu_freq.clear()
            self.lfu_buckets.clear()
            self.lfu_min_freq = 1
            self.ghost_lru.clear()
            self.ghost_lfu.clear()
            self.wlru = 1.0
            self.wlfu = 1.0
            self.miss_streak = 0
            self.last_evict_policy = "LRU"

    def _reset_if_new_run(self, cache_snapshot):
        # If access count resets or first call, re-sync structures.
        if self.last_access_seen > cache_snapshot.access_count or self.last_access_seen < 0 or cache_snapshot.access_count <= 1:
            self._ensure_capacity(cache_snapshot.capacity)
            self._sync_with_cache(cache_snapshot)
        self.last_access_seen = cache_snapshot.access_count

    def _sync_with_cache(self, cache_snapshot):
        # Bring LRU/LFU state in sync with actual cache contents.
        present = set(cache_snapshot.cache.keys())
        # Remove keys no longer present
        for k in list(self.lru.keys()):
            if k not in present:
                self._lfu_remove(k)
                self.lru.pop(k, None)
        # Add any missing present keys (seed as freq=1, MRU)
        for k in present:
            if k not in self.lru:
                self.lru[k] = None
                self.lru.move_to_end(k, last=True)
                self._lfu_add(k)

    # ---------- LFU operations ----------

    def _bucket(self, f: int) -> OrderedDict:
        od = self.lfu_buckets.get(f)
        if od is None:
            od = OrderedDict()
            self.lfu_buckets[f] = od
        return od

    def _lfu_add(self, key: str):
        # Insert with freq=1
        self.lfu_freq[key] = 1
        self._bucket(1)[key] = None
        self.lfu_min_freq = 1

    def _lfu_inc(self, key: str):
        f = self.lfu_freq.get(key)
        if f is None:
            # Not tracked (shouldn't happen if synced); add fresh
            self._lfu_add(key)
            return
        # Remove from old bucket
        b = self._bucket(f)
        if key in b:
            b.pop(key, None)
        nf = f + 1
        self.lfu_freq[key] = nf
        self._bucket(nf)[key] = None
        # Update min_freq if needed
        if f == self.lfu_min_freq and len(self._bucket(f)) == 0:
            # Advance min_freq to next non-empty bucket
            while self.lfu_min_freq in self.lfu_buckets and len(self._bucket(self.lfu_min_freq)) == 0:
                self.lfu_min_freq += 1

    def _lfu_remove(self, key: str):
        f = self.lfu_freq.pop(key, None)
        if f is None:
            return
        b = self._bucket(f)
        b.pop(key, None)
        # Adjust min_freq lazily
        if f == self.lfu_min_freq and len(b) == 0:
            while self.lfu_min_freq in self.lfu_buckets and len(self._bucket(self.lfu_min_freq)) == 0:
                self.lfu_min_freq += 1

    def _lfu_victim(self) -> str | None:
        # Ensure min_freq points to a non-empty bucket
        mf = self.lfu_min_freq
        # Find the next non-empty bucket
        while len(self.lfu_buckets.get(mf, OrderedDict())) == 0:
            # Recompute from map if possible
            if self.lfu_freq:
                mf = min(self.lfu_freq.values())
            else:
                return None
            if len(self.lfu_buckets.get(mf, OrderedDict())) > 0:
                break
            # If recomputed mf bucket still empty, try to move up
            mf += 1
            if mf > (max(self.lfu_buckets.keys()) if self.lfu_buckets else 1):
                break
        self.lfu_min_freq = mf
        bucket = self.lfu_buckets.get(self.lfu_min_freq)
        if not bucket:
            return None
        # LFU victim: LRU within min-freq bucket
        return next(iter(bucket)) if bucket else None

    # ---------- Ghost operations ----------

    def _ghost_add(self, ghost: OrderedDict, key: str):
        if key in ghost:
            ghost.pop(key, None)
        ghost[key] = None
        ghost.move_to_end(key, last=True)
        # Bound ghost size to capacity
        while len(ghost) > self.capacity:
            ghost.popitem(last=False)

    def _reward_policy(self, policy: str):
        # Multiplicative weights update with small learning rate
        if policy == "LRU":
            self.wlru *= (1.0 + self.lr)
            self.wlfu *= (1.0 - self.lr * 0.5)
        else:
            self.wlfu *= (1.0 + self.lr)
            self.wlru *= (1.0 - self.lr * 0.5)
        # Floor and normalize to avoid drift
        self.wlru = max(self.wlru, self.weight_floor)
        self.wlfu = max(self.wlfu, self.weight_floor)

    # ---------- core operations ----------

    def choose_victim(self, cache_snapshot, new_obj) -> str:
        self._ensure_capacity(cache_snapshot.capacity)
        self._reset_if_new_run(cache_snapshot)
        self._sync_with_cache(cache_snapshot)

        # Bias towards LRU under scan-like behavior
        scan_bias = 0.0
        if self.miss_streak > self.capacity // 2:
            scan_bias = 0.25  # modest shift toward LRU

        # Decide policy to use deterministically by comparing weights + scan bias
        wlru_eff = self.wlru * (1.0 + scan_bias)
        wlfu_eff = self.wlfu

        # Candidates
        cand_lru = next(iter(self.lru)) if self.lru else None
        cand_lfu = self._lfu_victim()

        # If one candidate missing, fallback to the other
        if cand_lru is None and cand_lfu is None:
            # Fallback to any cache key
            return next(iter(cache_snapshot.cache))
        if cand_lru is None:
            self.last_evict_policy = "LFU"
            return cand_lfu
        if cand_lfu is None:
            self.last_evict_policy = "LRU"
            return cand_lru

        # Both exist: prefer policy with higher effective weight
        if wlru_eff >= wlfu_eff:
            self.last_evict_policy = "LRU"
            return cand_lru
        else:
            self.last_evict_policy = "LFU"
            return cand_lfu

    def on_hit(self, cache_snapshot, obj):
        self._ensure_capacity(cache_snapshot.capacity)
        self._reset_if_new_run(cache_snapshot)
        k = obj.key

        # Reset miss streak on hit
        self.miss_streak = 0

        # LRU refresh
        if k in self.lru:
            self.lru.move_to_end(k, last=True)
        else:
            # Out-of-sync hit: add it
            self.lru[k] = None
            self.lru.move_to_end(k, last=True)
            self._lfu_add(k)

        # LFU increment
        self._lfu_inc(k)

    def on_insert(self, cache_snapshot, obj):
        self._ensure_capacity(cache_snapshot.capacity)
        self._reset_if_new_run(cache_snapshot)
        k = obj.key

        # Miss streak update (insert happens on miss)
        self.miss_streak += 1

        # Add to cache metadata: LRU MRU and LFU freq=1
        # Remove stale placements if any
        self.lru.pop(k, None)
        self._lfu_remove(k)
        # Insert
        self.lru[k] = None
        self.lru.move_to_end(k, last=True)
        self._lfu_add(k)

        # LeCaR learning: reward policy whose ghost contains this missed key
        rewarded = False
        if k in self.ghost_lru:
            self._reward_policy("LRU")
            self.ghost_lru.pop(k, None)
            rewarded = True
        if k in self.ghost_lfu:
            self._reward_policy("LFU")
            self.ghost_lfu.pop(k, None)
            rewarded = True

        # Light weight damping when no signal (optional)
        if not rewarded:
            # Nudge weights slightly toward balance to avoid lock-in
            avg = (self.wlru + self.wlfu) / 2.0
            self.wlru = max(self.weight_floor, 0.99 * self.wlru + 0.01 * avg)
            self.wlfu = max(self.weight_floor, 0.99 * self.wlfu + 0.01 * avg)

        # Bound ghosts (already bounded on insert into ghosts, but keep tidy)
        while len(self.ghost_lru) > self.capacity:
            self.ghost_lru.popitem(last=False)
        while len(self.ghost_lfu) > self.capacity:
            self.ghost_lfu.popitem(last=False)

    def on_evict(self, cache_snapshot, obj, evicted_obj):
        self._ensure_capacity(cache_snapshot.capacity)
        self._reset_if_new_run(cache_snapshot)
        k = evicted_obj.key

        # Remove from resident structures
        if k in self.lru:
            self.lru.pop(k, None)
        self._lfu_remove(k)

        # Place into appropriate ghost based on which policy decided the eviction
        if self.last_evict_policy == "LRU":
            self._ghost_add(self.ghost_lru, k)
        else:
            self._ghost_add(self.ghost_lfu, k)


# Singleton policy instance
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