# EVOLVE-BLOCK-START
"""Adaptive cache eviction using an ARC-like policy with ghost history.

Public API:
- evict(cache_snapshot, obj) -> key
- update_after_hit(cache_snapshot, obj)
- update_after_insert(cache_snapshot, obj)
- update_after_evict(cache_snapshot, obj, evicted_obj)
"""

from collections import OrderedDict

class _ArcPolicy:
    """
    ARC-like policy:
    - T1: recency list (seen once, resident)
    - T2: frequency list (seen >=2, resident)
    - B1: ghost of keys evicted from T1
    - B2: ghost of keys evicted from T2
    - p: target size for T1 (adaptive; 0..capacity)
    """

    __slots__ = (
        "T1", "T2", "B1", "B2",
        "p", "capacity", "_last_evicted_from"
    )

    def __init__(self):
        self.T1 = OrderedDict()
        self.T2 = OrderedDict()
        self.B1 = OrderedDict()
        self.B2 = OrderedDict()
        self.p = 0
        self.capacity = None
        self._last_evicted_from = None  # 'T1' or 'T2'

    # ---------- internal helpers ----------

    def _ensure_capacity(self, cap: int):
        # On first call or capacity change, reset safely.
        if self.capacity is None:
            self.capacity = max(int(cap), 1)
            return
        if self.capacity != cap:
            # Reset state to avoid inconsistencies if framework changes capacity.
            self.T1.clear(); self.T2.clear(); self.B1.clear(); self.B2.clear()
            self.p = 0
            self.capacity = max(int(cap), 1)

    def _is_in_cache(self, key: str, cache_snapshot) -> bool:
        return key in cache_snapshot.cache

    def _prune_stale_residents(self, cache_snapshot):
        # Drop keys that our policy still tracks but the cache no longer has.
        cache_keys = cache_snapshot.cache.keys()
        for k in list(self.T1.keys()):
            if k not in cache_keys:
                self.T1.pop(k, None)
        for k in list(self.T2.keys()):
            if k not in cache_keys:
                self.T2.pop(k, None)

    def _prune_ghosts(self):
        cap = self.capacity or 1
        # Bound each ghost list individually to capacity.
        while len(self.B1) > cap:
            self.B1.popitem(last=False)
        while len(self.B2) > cap:
            self.B2.popitem(last=False)

    def _touch_T1(self, key: str):
        # Place/move key to MRU end of T1
        self.T1[key] = None
        self.T1.move_to_end(key)

    def _touch_T2(self, key: str):
        # Place/move key to MRU end of T2
        self.T2[key] = None
        self.T2.move_to_end(key)

    def _move_T1_to_B1(self, key: str):
        self.T1.pop(key, None)
        self.B1[key] = None
        self.B1.move_to_end(key)

    def _move_T2_to_B2(self, key: str):
        self.T2.pop(key, None)
        self.B2[key] = None
        self.B2.move_to_end(key)

    # ---------- public hooks called by the cache framework ----------

    def choose_victim(self, cache_snapshot, new_obj) -> str:
        """
        ARC victim selection:
        Prefer evicting from T1 (recency) when T1 is large (>p) or incoming key
        has history in B2 and T1 == p; else from T2 (frequency).
        """
        self._ensure_capacity(cache_snapshot.capacity)
        self._prune_stale_residents(cache_snapshot)

        in_B2 = (new_obj.key in self.B2)

        # Decide which resident list to evict from
        choose_T1 = len(self.T1) > 0 and (len(self.T1) > self.p or (in_B2 and len(self.T1) == self.p))

        victim_key = None
        if choose_T1 and len(self.T1) > 0:
            victim_key = next(iter(self.T1))
            self._last_evicted_from = 'T1'
        elif len(self.T2) > 0:
            victim_key = next(iter(self.T2))
            self._last_evicted_from = 'T2'
        elif len(self.T1) > 0:
            victim_key = next(iter(self.T1))
            self._last_evicted_from = 'T1'
        else:
            # Fallback: unknown ordering, pick an arbitrary key from current cache.
            # This also handles situations where our state is cold but cache is warm.
            victim_key = next(iter(cache_snapshot.cache))
            self._last_evicted_from = 'T1'

        return victim_key

    def on_hit(self, cache_snapshot, obj):
        """Hit handling: promote to T2 if in T1; reorder within T2 if already there."""
        self._ensure_capacity(cache_snapshot.capacity)

        key = obj.key
        # Keep state robust to any desyncs:
        if key in self.T1:
            # Second touch: promote to T2
            self.T1.pop(key, None)
            self._touch_T2(key)
        elif key in self.T2:
            # Renew recency in T2
            self._touch_T2(key)
        else:
            # If our metadata missed this key, place it as frequent (it was a hit).
            self._touch_T2(key)

    def on_insert(self, cache_snapshot, obj):
        """
        Insert handling (called on miss after space made, if needed):
        - If key in B1: increase p (bias to recency) and insert into T2.
        - If key in B2: decrease p (bias to frequency) and insert into T2.
        - Else: insert into T1.
        """
        self._ensure_capacity(cache_snapshot.capacity)
        key = obj.key

        if key in self.B1:
            # Increase p toward recency
            delta = max(1, len(self.B2) // max(1, len(self.B1)))
            self.p = min(self.capacity, self.p + delta)
            self.B1.pop(key, None)
            self._touch_T2(key)
        elif key in self.B2:
            # Decrease p toward frequency
            delta = max(1, len(self.B1) // max(1, len(self.B2)))
            self.p = max(0, self.p - delta)
            self.B2.pop(key, None)
            self._touch_T2(key)
        else:
            # New key with no ghost history: start in T1
            self._touch_T1(key)

        # Ensure ghosts are bounded
        self._prune_ghosts()

    def on_evict(self, cache_snapshot, obj, evicted_obj):
        """
        Eviction handling: move the evicted resident to its corresponding ghost list.
        Maintain bounded ghost metadata and adapt later on insert.
        """
        self._ensure_capacity(cache_snapshot.capacity)
        evk = evicted_obj.key

        if evk in self.T1:
            self._move_T1_to_B1(evk)
        elif evk in self.T2:
            self._move_T2_to_B2(evk)
        else:
            # Fall back to our last decision if state was pruned.
            if self._last_evicted_from == 'T1':
                self.B1[evk] = None
                self.B1.move_to_end(evk)
            else:
                self.B2[evk] = None
                self.B2.move_to_end(evk)

        self._prune_ghosts()


# Single policy instance reused across calls
_policy = _ArcPolicy()


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