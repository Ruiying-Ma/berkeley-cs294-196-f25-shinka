# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

# Adaptive Segmented LRU with ARC-like ghost feedback.
# Live segments:
#  - Probation (recently inserted, not yet proven)
#  - Protected (proven frequent/reused)
# Ghost segments (no data, just metadata of evicted keys):
#  - m_ghost_probation (recent recency victims)
#  - m_ghost_protected (recent frequency victims)
# Target size of protected adapts based on ghost hits.
m_ts = dict()                  # key -> last access timestamp
m_probation = set()            # probation segment membership
m_protected = set()            # protected segment membership
m_ghost_probation = dict()     # key -> timestamp (ghost of probation)
m_ghost_protected = dict()     # key -> timestamp (ghost of protected)
m_target_protected = None      # target number of protected entries
m_last_capacity = None         # remember capacity to re-init target if it changes


def _init_targets(cache_snapshot):
    global m_target_protected, m_last_capacity
    cap = cache_snapshot.capacity or max(len(cache_snapshot.cache), 1)
    if m_target_protected is None or m_last_capacity != cap:
        # Start balanced
        m_target_protected = max(1, int(cap * 0.5))
        m_last_capacity = cap


def _oldest_key(candidates):
    # Return the key with the smallest timestamp among candidates
    return min(candidates, key=lambda k: m_ts.get(k, -1))


def _trim_ghosts(capacity):
    # Bound ghost lists to capacity (ARC heuristic)
    global m_ghost_probation, m_ghost_protected
    def trim(ghost):
        if len(ghost) <= capacity:
            return
        over = len(ghost) - capacity
        for _ in range(over):
            kmin = min(ghost, key=lambda k: ghost[k])
            ghost.pop(kmin, None)
    trim(m_ghost_probation)
    trim(m_ghost_protected)


def _enforce_protected_quota():
    # Demote LRU from protected to probation until target is met
    global m_probation, m_protected
    while m_target_protected is not None and len(m_protected) > m_target_protected:
        demote_key = _oldest_key(m_protected)
        m_protected.discard(demote_key)
        m_probation.add(demote_key)


def evict(cache_snapshot, obj):
    '''
    This function defines how the algorithm chooses the eviction victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The new object that needs to be inserted into the cache.
    - Return:
        - `candid_obj_key`: The key of the cached object that will be evicted to make room for `obj`.
    '''
    global m_ts, m_probation, m_protected
    _init_targets(cache_snapshot)

    keys_in_cache = set(cache_snapshot.cache.keys())

    # Keep metadata consistent with actual cache content
    if m_probation:
        m_probation.intersection_update(keys_in_cache)
    if m_protected:
        m_protected.intersection_update(keys_in_cache)
    if m_ts:
        for k in list(m_ts.keys()):
            if k not in keys_in_cache:
                m_ts.pop(k, None)
                m_probation.discard(k)
                m_protected.discard(k)

    probation_candidates = m_probation & keys_in_cache
    protected_candidates = m_protected & keys_in_cache

    # Prefer evicting from probationary segment to avoid polluting protected items
    if probation_candidates:
        return _oldest_key(probation_candidates)
    if protected_candidates:
        return _oldest_key(protected_candidates)

    # Fallback: evict the globally oldest if segmentation hasn't been set yet
    if keys_in_cache:
        return _oldest_key(keys_in_cache)
    return None


def update_after_hit(cache_snapshot, obj):
    '''
    This function defines how the algorithm update the metadata it maintains immediately after a cache hit.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object accessed during the cache hit.
    - Return: `None`
    '''
    global m_ts, m_probation, m_protected, m_target_protected
    _init_targets(cache_snapshot)
    now = cache_snapshot.access_count
    key = obj.key

    # Ensure timestamp exists and update recency
    m_ts[key] = now

    if key in m_probation:
        # Promote on first reuse
        m_probation.discard(key)
        m_protected.add(key)
        # Slightly increase protected target on successful promotion (favor frequency)
        cap = m_last_capacity or max(len(cache_snapshot.cache), 1)
        delta = 1  # conservative step to avoid oscillation
        m_target_protected = min(cap, max(1, m_target_protected + delta))
    elif key not in m_protected:
        # If metadata was missing, treat as protected to avoid premature eviction
        m_protected.add(key)

    # Enforce protected quota by demoting its LRU if needed
    _enforce_protected_quota()


def update_after_insert(cache_snapshot, obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after inserting a new object into the cache.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object that was just inserted into the cache.
    - Return: `None`
    '''
    global m_ts, m_probation, m_protected, m_ghost_probation, m_ghost_protected, m_target_protected
    _init_targets(cache_snapshot)
    now = cache_snapshot.access_count
    key = obj.key
    cap = m_last_capacity or max(len(cache_snapshot.cache), 1)

    # ARC-like adaptation based on ghost hits:
    # - If miss corresponds to probation ghost, favor recency (shrink protected target)
    # - If miss corresponds to protected ghost, favor frequency (grow protected target)
    step = max(1, cap // 32)
    if key in m_ghost_probation:
        m_target_protected = max(1, m_target_protected - step)
        m_ghost_probation.pop(key, None)
    elif key in m_ghost_protected:
        m_target_protected = min(cap, m_target_protected + step)
        m_ghost_protected.pop(key, None)

    # Insert starts in probation
    m_ts[key] = now
    m_protected.discard(key)
    m_probation.add(key)

    # Respect current target by demoting protected LRU if over target
    _enforce_protected_quota()

    # Keep ghost lists bounded
    _trim_ghosts(cap)


def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after evicting the victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object to be inserted into the cache.
        - `evicted_obj`: The object that was just evicted from the cache.
    - Return: `None`
    '''
    global m_ts, m_probation, m_protected, m_ghost_probation, m_ghost_protected
    _init_targets(cache_snapshot)
    evk = evicted_obj.key
    now = cache_snapshot.access_count
    cap = m_last_capacity or max(len(cache_snapshot.cache), 1)

    # Determine segment before removal
    was_protected = evk in m_protected
    was_probation = evk in m_probation

    # Remove all metadata for the evicted object
    m_ts.pop(evk, None)
    m_probation.discard(evk)
    m_protected.discard(evk)

    # Record into appropriate ghost list (ARC feedback)
    if was_protected:
        m_ghost_protected[evk] = now
    else:
        # If unknown or probation, treat as probation ghost
        m_ghost_probation[evk] = now

    # Trim ghosts to capacity
    _trim_ghosts(cap)

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