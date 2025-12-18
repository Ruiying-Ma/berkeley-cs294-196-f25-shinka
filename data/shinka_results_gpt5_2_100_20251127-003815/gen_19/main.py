# EVOLVE-BLOCK-START
"""Adaptive SLRU+LRFU eviction: probation/protected segments with exponential-decay score + TinyLFU-guided promotion and scan guard"""

# Configuration: half-life in number of accesses for score decay.
# After DECAY_HALF_LIFE accesses without a hit, a key's score halves.
DECAY_HALF_LIFE = 8
DECAY_BASE = 2 ** (-1.0 / DECAY_HALF_LIFE)

# TinyLFU promotion threshold
_PROMOTE_THRESHOLD = 2

# Per-key metadata for cached objects
_key_score = dict()      # key -> float decayed frequency score
_key_last_time = dict()  # key -> int last access_count when we updated its score

# Segmented resident sets
_probation = set()       # keys admitted recently or with single hit
_protected = set()       # keys with demonstrated frequency
_prot_cap = 0            # adaptive target size (in number of keys) for protected segment

# Simple scan detector
_miss_streak = 0
_scan_cooldown = 0

class _CmSketch:
    """
    Count-Min Sketch with conservative aging for TinyLFU-like frequency estimates.
    """
    __slots__ = ("d", "w", "tables", "mask", "ops", "age_period", "seeds")

    def __init__(self, width_power=12, d=4):
        self.d = int(max(1, d))
        w = 1 << int(max(8, width_power))  # min width 256
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

_sketch = _CmSketch()

def _prune_membership(cache_snapshot):
    """Remove keys from segment sets that are no longer resident in the cache."""
    cache_keys = set(cache_snapshot.cache.keys())
    for seg in (_probation, _protected):
        stale = [k for k in seg if k not in cache_keys]
        for k in stale:
            seg.discard(k)

def _ensure_meta(k, now):
    """Ensure metadata exists for key k and lazily decay its score to 'now'."""
    if k not in _key_last_time:
        _key_last_time[k] = now
    if k not in _key_score:
        _key_score[k] = 0.0
    dt = now - _key_last_time[k]
    if dt > 0:
        _key_score[k] *= pow(DECAY_BASE, dt)
        _key_last_time[k] = now
    return _key_score[k], _key_last_time[k]

def _pick_min_by_score(candidates, now):
    """Pick key with minimal decayed score from candidates; tie-break on older last access."""
    min_key = None
    min_score = None
    min_old_time = None
    for k in candidates:
        old_time = _key_last_time.get(k, now)
        s, _ = _ensure_meta(k, now)
        if (min_score is None) or (s < min_score) or (s == min_score and old_time < (min_old_time if min_old_time is not None else old_time)):
            min_score = s
            min_key = k
            min_old_time = old_time
    return min_key

def _enforce_protected_cap(now):
    """If protected exceeds target cap, demote lowest-score protected key to probation."""
    global _prot_cap
    while _protected and len(_protected) > _prot_cap:
        k = _pick_min_by_score(_protected, now)
        if k is None:
            break
        _protected.discard(k)
        _probation.add(k)

def evict(cache_snapshot, obj):
    '''
    This function defines how the algorithm chooses the eviction victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The new object that needs to be inserted into the cache.
    - Return:
        - `candid_obj_key`: The key of the cached object that will be evicted to make room for `obj`.
    '''
    global _prot_cap
    now = cache_snapshot.access_count

    # Keep sets consistent with actual cache content
    _prune_membership(cache_snapshot)

    # Initialize protected capacity target if unset
    cur_cap = max(1, len(cache_snapshot.cache))
    if _prot_cap <= 0:
        _prot_cap = max(1, cur_cap // 2)

    # Prefer evicting from probation; if empty, from protected; else fallback to any key
    prob_candidates = [k for k in _probation if k in cache_snapshot.cache]
    if prob_candidates:
        return _pick_min_by_score(prob_candidates, now)

    prot_candidates = [k for k in _protected if k in cache_snapshot.cache]
    if prot_candidates:
        return _pick_min_by_score(prot_candidates, now)

    # Fallback: choose globally minimal score among resident keys
    return _pick_min_by_score(list(cache_snapshot.cache.keys()), now)

def update_after_hit(cache_snapshot, obj):
    '''
    This function defines how the algorithm update the metadata it maintains immediately after a cache hit.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object accessed during the cache hit.
    - Return: `None`
    '''
    global _prot_cap, _miss_streak, _scan_cooldown
    now = cache_snapshot.access_count

    _prune_membership(cache_snapshot)

    # Initialize protected capacity target if unset
    cur_cap = max(1, len(cache_snapshot.cache))
    if _prot_cap <= 0:
        _prot_cap = max(1, cur_cap // 2)

    # Track frequency globally
    _sketch.increment(obj.key, 1)

    # Update decayed score and last time
    _ensure_meta(obj.key, now)

    # Reset miss streak and cooldown tick
    _miss_streak = 0
    if _scan_cooldown > 0:
        _scan_cooldown -= 1

    # Promotion logic guided by TinyLFU estimate
    est = _sketch.estimate(obj.key)
    if obj.key in _probation:
        if est >= _PROMOTE_THRESHOLD:
            _probation.discard(obj.key)
            _protected.add(obj.key)
            # Bias toward larger protected region only on meaningful promotions
            if _prot_cap < cur_cap:
                _prot_cap += 1
        else:
            # Remain in probation; no cap growth
            pass
    else:
        # Unknown or already protected: ensure protected membership for stable frequent keys
        _protected.add(obj.key)
        _probation.discard(obj.key)

    # Frequency boost; slightly stronger in protected
    if obj.key in _protected:
        _key_score[obj.key] += 1.25
    else:
        _key_score[obj.key] += 0.75

    # Enforce protected capacity by demoting weakest protected key
    _enforce_protected_cap(now)

def update_after_insert(cache_snapshot, obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after inserting a new object into the cache.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object that was just inserted into the cache.
    - Return: `None`
    '''
    global _prot_cap, _miss_streak, _scan_cooldown
    now = cache_snapshot.access_count

    _prune_membership(cache_snapshot)

    # Initialize protected capacity on cold start
    cur_cap = max(1, len(cache_snapshot.cache))
    if _prot_cap <= 0:
        _prot_cap = max(1, cur_cap // 2)

    # Record this access into the TinyLFU sketch
    _sketch.increment(obj.key, 1)
    est = _sketch.estimate(obj.key)

    # Simple scan detector: long miss streak => shrink protected target, with cooldown
    _miss_streak += 1
    if _scan_cooldown > 0:
        _scan_cooldown -= 1
    if _miss_streak > (2 * cur_cap) and _scan_cooldown == 0:
        _prot_cap = max(1, _prot_cap // 2)
        _scan_cooldown = cur_cap

    # Admission: hot-admit into protected if estimate suggests frequency, else probation
    if est >= _PROMOTE_THRESHOLD:
        _protected.add(obj.key)
        _probation.discard(obj.key)
        _key_last_time[obj.key] = now
        _key_score[obj.key] = 0.6  # modest starting score; hits will boost quickly
    else:
        _probation.add(obj.key)
        _protected.discard(obj.key)
        _key_last_time[obj.key] = now
        _key_score[obj.key] = 0.1  # small to limit scan pollution

    # Enforce protected capacity after potential hot admission
    _enforce_protected_cap(now)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after evicting the victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object to be inserted into the cache.
        - `evicted_obj`: The object that was just evicted from the cache.
    - Return: `None`
    '''
    global _prot_cap
    # Clear all metadata for the evicted key
    _key_score.pop(evicted_obj.key, None)
    _key_last_time.pop(evicted_obj.key, None)
    _probation.discard(evicted_obj.key)
    _protected.discard(evicted_obj.key)

    # Make sure protected cap does not exceed current possible size
    cur_cap = max(1, len(cache_snapshot.cache))
    if _prot_cap > cur_cap:
        _prot_cap = cur_cap

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