# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

# Legacy timestamp dictionary kept for compatibility; used as a general access ledger.
m_key_timestamp = dict()

# Segmented LRU metadata: probation and protected segments (key -> last access time)
_probation = dict()
_protected = dict()

def _get_caps(cache_snapshot):
    """Compute target sizes for probation and protected segments."""
    total_cap = max(int(getattr(cache_snapshot, "capacity", 1)), 1)
    # Favor protected segment to keep repeatedly used items
    prot_cap = max(int(total_cap * 0.66), 1 if total_cap > 1 else 0)
    prob_cap = max(total_cap - prot_cap, 1)
    return prob_cap, prot_cap

def _lru_key_in(seg_dict, cache_snapshot):
    """Return the LRU key from seg_dict that is currently in the cache."""
    min_key = None
    min_ts = None
    # Iterate only over keys that are in the current cache snapshot
    cache_keys = cache_snapshot.cache.keys()
    for k, ts in seg_dict.items():
        if k in cache_keys:
            if (min_ts is None) or (ts < min_ts):
                min_ts = ts
                min_key = k
    return min_key

def evict(cache_snapshot, obj):
    '''
    This function defines how the algorithm chooses the eviction victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The new object that needs to be inserted into the cache.
    - Return:
        - `candid_obj_key`: The key of the cached object that will be evicted to make room for `obj`.
    '''
    # Prefer evicting from the probation segment to protect re-referenced items.
    candid_obj_key = _lru_key_in(_probation, cache_snapshot)
    if candid_obj_key is None:
        # Fall back to protected segment if probation has no candidates in cache.
        candid_obj_key = _lru_key_in(_protected, cache_snapshot)
    if candid_obj_key is None:
        # Last-resort fallback: pick any key from the cache (should rarely happen).
        for k in cache_snapshot.cache:
            candid_obj_key = k
            break
    return candid_obj_key

def update_after_hit(cache_snapshot, obj):
    '''
    This function defines how the algorithm update the metadata it maintains immediately after a cache hit.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object accessed during the cache hit.
    - Return: `None`
    '''
    global m_key_timestamp, _probation, _protected
    current_ts = cache_snapshot.access_count
    k = obj.key

    # Maintain a general timestamp ledger for robustness.
    m_key_timestamp[k] = current_ts

    if k in _probation:
        # Promote to protected on second touch.
        _probation.pop(k, None)
        _protected[k] = current_ts

        # If protected exceeds capacity, demote its LRU back to probation.
        prob_cap, prot_cap = _get_caps(cache_snapshot)
        if len(_protected) > prot_cap:
            demote_key = _lru_key_in(_protected, cache_snapshot)
            if demote_key is not None and demote_key != k:
                demote_ts = _protected.pop(demote_key, current_ts)
                # Keep original timestamp to maintain proper LRU ordering in probation.
                _probation[demote_key] = demote_ts
    elif k in _protected:
        # Refresh recency within protected.
        _protected[k] = current_ts
    else:
        # Metadata miss: treat as a re-reference and place in protected.
        _protected[k] = current_ts
        prob_cap, prot_cap = _get_caps(cache_snapshot)
        if len(_protected) > prot_cap:
            demote_key = _lru_key_in(_protected, cache_snapshot)
            if demote_key is not None and demote_key != k:
                demote_ts = _protected.pop(demote_key, current_ts)
                _probation[demote_key] = demote_ts

def update_after_insert(cache_snapshot, obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after inserting a new object into the cache.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object that was just inserted into the cache.
    - Return: `None`
    '''
    global m_key_timestamp, _probation, _protected
    current_ts = cache_snapshot.access_count
    k = obj.key

    # Record in general ledger and place new entries in the probation segment.
    m_key_timestamp[k] = current_ts
    _probation[k] = current_ts

    # Optional hygiene: if probation grows too large relative to capacity, no immediate action is required.
    # Eviction will naturally prefer probation, which is desired.

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after evicting the victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object to be inserted into the cache.
        - `evicted_obj`: The object that was just evicted from the cache.
    - Return: `None`
    '''
    global m_key_timestamp, _probation, _protected
    # Remove evicted key from all metadata stores.
    if evicted_obj is not None:
        _probation.pop(evicted_obj.key, None)
        _protected.pop(evicted_obj.key, None)
        m_key_timestamp.pop(evicted_obj.key, None)
    # Do not add obj here; it will be handled in update_after_insert.

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