# EVOLVE-BLOCK-START
"""Cache eviction algorithm: SLRU + TinyLFU-guided victim selection"""

from collections import OrderedDict

# Global timestamps for tie-breaking/fallback
m_key_timestamp = dict()

# SLRU segments
slru_probation = OrderedDict()  # probationary segment (new/cold)
slru_protected = OrderedDict()  # protected segment (hot)
slru_capacity = None
slru_protected_max = None  # target size for protected segment

# TinyLFU Count-Min Sketch
cms_depth = 4
cms_width = 2048
cms_mask = cms_width - 1
cms_tables = None
cms_additions = 0
cms_decay_interval = 4096  # number of increments between decays


def _next_pow2(x: int) -> int:
    v = 1
    while v < x:
        v <<= 1
    return max(1, v)


def _cms_init():
    global cms_tables, cms_width, cms_mask, cms_decay_interval
    if cms_tables is None:
        cms_tables = [[0] * cms_width for _ in range(cms_depth)]
        cms_mask = cms_width - 1
        # decay interval tuned to capacity scale (done in _ensure_capacity)
        # already set by _ensure_capacity


def _cms_reinit_for_capacity(cap: int):
    """Reinitialize CMS parameters when capacity is first known."""
    global cms_tables, cms_width, cms_mask, cms_decay_interval
    # Width proportional to capacity; power of two for fast masking
    cms_width = min(16384, max(1024, _next_pow2(cap * 2)))
    cms_mask = cms_width - 1
    cms_tables = [[0] * cms_width for _ in range(cms_depth)]
    # Decay about every few caches of operations to bound counters
    cms_decay_interval = max(2 * cms_width, cap * 8)


def _cms_idx(i, key):
    # Mix hashes per depth; use mask for modulo
    h1 = hash(key)
    h2 = hash((i + 0x9e3779b1, key))
    return (h1 ^ (h2 << 1) ^ (h1 >> (i + 1))) & cms_mask


def _cms_inc(key):
    global cms_additions
    if cms_tables is None:
        _cms_init()
    for i in range(cms_depth):
        idx = _cms_idx(i, key)
        # Saturate at 2^31-1 to avoid overflow
        v = cms_tables[i][idx] + 1
        cms_tables[i][idx] = v if v < 0x7FFFFFFF else 0x7FFFFFFF
    cms_additions += 1
    if cms_additions % cms_decay_interval == 0:
        # Periodic halving decay
        for i in range(cms_depth):
            row = cms_tables[i]
            for j in range(cms_width):
                row[j] >>= 1


def _cms_estimate(key) -> int:
    if cms_tables is None:
        _cms_init()
    est = None
    for i in range(cms_depth):
        idx = _cms_idx(i, key)
        v = cms_tables[i][idx]
        est = v if est is None else (v if v < est else est)
    return est if est is not None else 0


def _ensure_capacity(cache_snapshot):
    """Initialize capacity-dependent parameters once."""
    global slru_capacity, slru_protected_max
    if slru_capacity is None:
        slru_capacity = max(int(cache_snapshot.capacity), 1)
        # Protected gets 80%, probation gets 20% by default
        slru_protected_max = max(1, int(slru_capacity * 0.8))
        _cms_reinit_for_capacity(slru_capacity)


def _move_to_mru(od, key):
    if key in od:
        od.pop(key, None)
    od[key] = True


def _pop_lru(od):
    if not od:
        return None
    k, _ = od.popitem(last=False)
    return k


def _resync(cache_snapshot):
    """Ensure SLRU metadata matches actual cache contents."""
    cache_keys = set(cache_snapshot.cache.keys())
    # Remove non-resident keys from segments
    for k in list(slru_probation.keys()):
        if k not in cache_keys:
            slru_probation.pop(k, None)
    for k in list(slru_protected.keys()):
        if k not in cache_keys:
            slru_protected.pop(k, None)
    # Any missing resident keys become probationary
    for k in cache_keys:
        if k not in slru_probation and k not in slru_protected:
            slru_probation[k] = True
    # Enforce protected max by demoting LRU protected if oversized
    while len(slru_protected) > (slru_protected_max if slru_protected_max is not None else len(slru_protected)):
        demote = _pop_lru(slru_protected)
        if demote is not None:
            _move_to_mru(slru_probation, demote)


def _choose_victim_from_probation(sample_k: int, now: int):
    """Pick the lowest estimated frequency among the k-elder probation entries."""
    if not slru_probation:
        return None
    # Gather up to sample_k from probation LRU side
    victims = []
    i = 0
    for k in slru_probation.keys():
        victims.append(k)
        i += 1
        if i >= sample_k:
            break
    # Choose by minimum TinyLFU estimate, tie-break by oldest timestamp
    best = None
    best_score = None
    best_time = None
    for k in victims:
        s = _cms_estimate(k)
        ts = m_key_timestamp.get(k, 0)
        if best is None or s < best_score or (s == best_score and ts < best_time):
            best = k
            best_score = s
            best_time = ts
    return best


def evict(cache_snapshot, obj):
    """
    Choose eviction victim using SLRU + TinyLFU:
    - Prefer evicting from probationary LRU set; sample a few LRU candidates and evict the one with lowest estimated frequency.
    - If probation is empty, demote one protected LRU to probation and then evict from probation.
    """
    _ensure_capacity(cache_snapshot)
    _resync(cache_snapshot)
    now = cache_snapshot.access_count

    # Try sampling from probation
    sample_k = max(2, min(8, slru_capacity // 16))  # small, bounded sample
    victim = _choose_victim_from_probation(sample_k, now)

    # If probation empty, demote from protected then retry
    if victim is None:
        demote = _pop_lru(slru_protected)
        if demote is not None:
            _move_to_mru(slru_probation, demote)
            victim = _choose_victim_from_probation(sample_k, now)

    # Fallbacks if metadata empty or drifted
    if victim is None:
        _resync(cache_snapshot)
        # Try again
        victim = _choose_victim_from_probation(sample_k, now)
    if victim is None:
        # As last resort, evict oldest in cache by timestamp
        if cache_snapshot.cache:
            # Find key with minimum timestamp
            victim = None
            oldest_ts = None
            for k in cache_snapshot.cache.keys():
                ts = m_key_timestamp.get(k, 0)
                if victim is None or ts < oldest_ts:
                    victim = k
                    oldest_ts = ts
        else:
            victim = None
    return victim


def update_after_hit(cache_snapshot, obj):
    """
    On cache hit:
    - Increment TinyLFU counter.
    - If in probation: promote to protected MRU; if protected exceeds its target, demote its LRU to probation MRU.
    - If in protected: refresh to MRU.
    - If not tracked (drift): add to probation MRU.
    """
    _ensure_capacity(cache_snapshot)
    _resync(cache_snapshot)
    now = cache_snapshot.access_count
    key = obj.key

    _cms_inc(key)

    if key in slru_protected:
        _move_to_mru(slru_protected, key)
    elif key in slru_probation:
        # Promote to protected
        slru_probation.pop(key, None)
        _move_to_mru(slru_protected, key)
        # Keep protected within cap via demotion of its LRU
        if slru_protected_max is not None and len(slru_protected) > slru_protected_max:
            demote = _pop_lru(slru_protected)
            if demote is not None:
                _move_to_mru(slru_probation, demote)
    else:
        # Drift: place as probationary
        _move_to_mru(slru_probation, key)

    m_key_timestamp[key] = now


def update_after_insert(cache_snapshot, obj):
    """
    After a miss and insertion:
    - Increment TinyLFU counter.
    - Place the new key into probation MRU.
    """
    _ensure_capacity(cache_snapshot)
    _resync(cache_snapshot)
    now = cache_snapshot.access_count
    key = obj.key

    _cms_inc(key)
    _move_to_mru(slru_probation, key)
    m_key_timestamp[key] = now


def update_after_evict(cache_snapshot, obj, evicted_obj):
    """
    After eviction:
    - Remove the evicted key from SLRU segments and timestamp map.
    """
    _ensure_capacity(cache_snapshot)
    k = evicted_obj.key
    slru_probation.pop(k, None)
    slru_protected.pop(k, None)
    m_key_timestamp.pop(k, None)
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