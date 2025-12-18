# EVOLVE-BLOCK-START
"""Adaptive, modular cache eviction policy based on ARC with ghost lists.

Structural redesign:
- Encapsulate all policy state/logic in ARCPolicy class.
- Maintain four OrderedDict segments: T1/T2 (resident), B1/B2 (ghosts).
- Adaptive balancing parameter p tunes space split between T1 and T2.
- REPLACE selects eviction side based on p and presence in B2 (ARC rule).
- update_after_* keep metadata in sync after cache events.

Why this helps:
- ARC adapts to recency vs. frequency dynamically, resisting scans while keeping hot items.
- Ghost lists capture recently evicted items to guide p updates, improving phase shift response.
- OrderedDict yields efficient LRU behavior with clean modularity.
"""

from collections import OrderedDict

class ARCPolicy:
    def __init__(self):
        # Resident LRU lists
        self.T1 = OrderedDict()  # probationary (seen once)
        self.T2 = OrderedDict()  # protected (reused)
        # Ghost (non-resident) LRU lists
        self.B1 = OrderedDict()  # evicted from T1
        self.B2 = OrderedDict()  # evicted from T2

        # Adaptive target for T1 size [0, c]
        self.p = 0.0

        # Metadata
        self.ts = {}            # key -> last access timestamp (for fallback)
        self.freq = {}          # key -> tiny frequency counter (optional boost)
        self.last_capacity = None

    # ---------- Internal utilities ----------
    def _capacity(self, cache_snapshot):
        cap = cache_snapshot.capacity or max(len(cache_snapshot.cache), 1)
        return max(int(cap), 1)

    def _init_or_update(self, cache_snapshot):
        c = self._capacity(cache_snapshot)
        if self.last_capacity != c:
            # Clamp p and trim ghost sizes to 2c after capacity change
            self.p = max(0.0, min(self.p, float(c)))
            self._trim_ghosts_to(2 * c)
            self.last_capacity = c
        # Sync resident lists with actual cache keys (defensive)
        self._sync_with_cache(cache_snapshot)

    def _sync_with_cache(self, cache_snapshot):
        keys_in_cache = set(cache_snapshot.cache.keys())
        # Remove any resident entries not actually in cache
        for od in (self.T1, self.T2):
            to_remove = [k for k in od.keys() if k not in keys_in_cache]
            for k in to_remove:
                od.pop(k, None)
        # Timestamps cleanup for non-present keys
        for k in list(self.ts.keys()):
            if k not in keys_in_cache and k not in self.B1 and k not in self.B2:
                self.ts.pop(k, None)
                self.freq.pop(k, None)

    def _trim_ghosts_to(self, max_total):
        # Ensure |T1| + |T2| + |B1| + |B2| <= max_total by trimming oldest ghosts
        while (len(self.T1) + len(self.T2) + len(self.B1) + len(self.B2)) > max_total:
            if self.B1:
                self.B1.popitem(last=False)
            elif self.B2:
                self.B2.popitem(last=False)
            else:
                break

    def _lru_key(self, odict: OrderedDict):
        # Return LRU key (oldest) if exists, else None
        try:
            return next(iter(odict))
        except StopIteration:
            return None

    def _replace_side(self, obj_key, c):
        # ARC REPLACE rule: choose eviction from T1 or T2
        len_t1 = len(self.T1)
        p_int = int(round(self.p))
        # If |T1| > p, evict from T1; if |T1| == p and obj ∈ B2, evict from T1; else from T2
        if len_t1 > p_int or (len_t1 == p_int and obj_key in self.B2):
            return 'T1'
        else:
            return 'T2'

    def _record_access(self, key, now):
        self.ts[key] = now
        self.freq[key] = min(self.freq.get(key, 0) + 1, 7)  # tiny saturating counter

    # ---------- Public policy interface ----------
    def evict(self, cache_snapshot, obj):
        self._init_or_update(cache_snapshot)
        c = self._capacity(cache_snapshot)

        # Choose victim according to ARC REPLACE, but ensure we pick a key actually in cache
        keys_in_cache = set(cache_snapshot.cache.keys())

        # If both resident lists empty (should not happen often), fallback to global oldest
        if not self.T1 and not self.T2:
            # Fallback: evict globally oldest (by timestamp) among actual cache keys
            if not keys_in_cache:
                return None
            # If we have timestamps, use them; else arbitrary
            candidates = list(keys_in_cache)
            if self.ts:
                return min(candidates, key=lambda k: self.ts.get(k, -1))
            return next(iter(candidates))

        side = self._replace_side(obj.key, c)
        victim = None
        if side == 'T1' and self.T1:
            victim = self._lru_key(self.T1)
        elif side == 'T2' and self.T2:
            victim = self._lru_key(self.T2)
        else:
            # If chosen side empty (edge case), pick from the other; else any cached key
            if self.T1:
                victim = self._lru_key(self.T1)
            elif self.T2:
                victim = self._lru_key(self.T2)

        # Defensive: ensure victim is in actual cache; otherwise fallback to any cached key
        if victim is None or victim not in keys_in_cache:
            # Try other list
            alt = self._lru_key(self.T2 if side == 'T1' else self.T1)
            if alt and alt in keys_in_cache:
                victim = alt
            else:
                # Fallback: evict globally oldest
                if not keys_in_cache:
                    return None
                candidates = list(keys_in_cache)
                if self.ts:
                    victim = min(candidates, key=lambda k: self.ts.get(k, -1))
                else:
                    victim = next(iter(candidates))
        return victim

    def update_after_hit(self, cache_snapshot, obj):
        self._init_or_update(cache_snapshot)
        now, key = cache_snapshot.access_count, obj.key
        self._record_access(key, now)

        # If in T1, promote to T2 MRU
        if key in self.T1:
            self.T1.pop(key, None)
            self.T2[key] = True
        elif key in self.T2:
            # Refresh recency
            self.T2.move_to_end(key, last=True)
        else:
            # Not tracked (metadata loss or sync), treat as protected to avoid premature eviction
            self.T2[key] = True
        # Keep resident sizes reasonable; if we somehow exceeded, demote from T2 to T1
        c = self._capacity(cache_snapshot)
        if (len(self.T1) + len(self.T2)) > c and self.T2:
            # Demote oldest of T2 to T1 to preserve capacity accounting symmetry
            demote = self._lru_key(self.T2)
            if demote is not None:
                self.T2.pop(demote, None)
                self.T1[demote] = True

    def update_after_insert(self, cache_snapshot, obj):
        self._init_or_update(cache_snapshot)
        now, key = cache_snapshot.access_count, obj.key
        self._record_access(key, now)

        c = self._capacity(cache_snapshot)

        # If key was in ghost lists, adapt p and place into T2 (ARC)
        if key in self.B1:
            # Increase p
            delta = max(1.0, len(self.B2) / float(max(1, len(self.B1))))
            self.p = min(float(c), self.p + delta)
            # Move from B1 to resident protected
            self.B1.pop(key, None)
            self.T2[key] = True
        elif key in self.B2:
            # Decrease p
            delta = max(1.0, len(self.B1) / float(max(1, len(self.B2))))
            self.p = max(0.0, self.p - delta)
            # Move from B2 to resident protected
            self.B2.pop(key, None)
            self.T2[key] = True
        else:
            # New key: insert into T1 (probationary)
            self.T1[key] = True

        # Ensure |T1| + |T2| <= c by demoting if needed (defensive)
        if (len(self.T1) + len(self.T2)) > c:
            side = 'T1' if len(self.T1) > int(round(self.p)) else 'T2'
            if side == 'T1' and self.T1:
                k = self._lru_key(self.T1)
                if k is not None:
                    self.T1.pop(k, None)
                    self.B1[k] = True
            elif self.T2:
                k = self._lru_key(self.T2)
                if k is not None:
                    self.T2.pop(k, None)
                    self.B2[k] = True

        # Trim ghosts: |T1| + |T2| + |B1| + |B2| <= 2c
        self._trim_ghosts_to(2 * c)

    def update_after_evict(self, cache_snapshot, obj, evicted_obj):
        # Move evicted resident to appropriate ghost list
        evk = evicted_obj.key

        # Remove from resident lists if present and add to respective ghost
        if evk in self.T1:
            self.T1.pop(evk, None)
            self.B1[evk] = True
        elif evk in self.T2:
            self.T2.pop(evk, None)
            self.B2[evk] = True
        else:
            # If not in resident lists, just clear from ghosts to avoid overgrowth
            self.B1.pop(evk, None)
            self.B2.pop(evk, None)

        # Cleanup tiny metadata; keep timestamp for ghosts only if we want, but keep simple
        self.freq.pop(evk, None)
        # Bound ghosts
        c = self._capacity(cache_snapshot)
        self._trim_ghosts_to(2 * c)


# Global policy instance
_policy = ARCPolicy()

def evict(cache_snapshot, obj):
    '''
    Return the key of the cached object that will be evicted to make room for `obj`.
    '''
    return _policy.evict(cache_snapshot, obj)

def update_after_hit(cache_snapshot, obj):
    '''
    Update metadata immediately after a cache hit.
    '''
    _policy.update_after_hit(cache_snapshot, obj)

def update_after_insert(cache_snapshot, obj):
    '''
    Update metadata immediately after inserting a new object into the cache.
    '''
    _policy.update_after_insert(cache_snapshot, obj)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Update metadata immediately after evicting the victim.
    '''
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