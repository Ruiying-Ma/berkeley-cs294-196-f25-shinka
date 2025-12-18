# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

from collections import OrderedDict

# Adaptive Replacement Cache (ARC)-inspired:
# - m_probation (T1): recent entries (LRU)
# - m_protected (T2): frequent/hot entries (LRU)
# - m_b1 (B1): ghost list of keys evicted from T1 (LRU)
# - m_b2 (B2): ghost list of keys evicted from T2 (LRU)
# - m_p: adaptive target size for T1
m_probation = OrderedDict()
m_protected = OrderedDict()
m_b1 = OrderedDict()
m_b2 = OrderedDict()
m_freq = dict()
m_p = 0
_m_last_seen_access = -1  # detect new traces to reset metadata


def _reset_if_new_run(cache_snapshot):
    """Reset metadata when a new trace/cache run starts."""
    global m_probation, m_protected, m_b1, m_b2, m_freq, m_p, _m_last_seen_access
    # New run if access counter restarts or at very beginning
    if cache_snapshot.access_count <= 1 or _m_last_seen_access > cache_snapshot.access_count:
        m_probation.clear()
        m_protected.clear()
        m_b1.clear()
        m_b2.clear()
        m_freq.clear()
        m_p = 0
    _m_last_seen_access = cache_snapshot.access_count


def _prune_metadata(cache_snapshot):
    """Keep metadata consistent with actual cache content."""
    cache_keys = cache_snapshot.cache.keys()
    for seg in (m_probation, m_protected):
        to_del = [k for k in seg.keys() if k not in cache_keys]
        for k in to_del:
            seg.pop(k, None)


def _protected_target_size(cache_snapshot):
    """Aim to keep most entries protected while leaving room in probation."""
    cap = max(int(cache_snapshot.capacity), 1)
    return max(1, int(cap * 0.8))


def evict(cache_snapshot, obj):
    '''
    This function defines how the algorithm chooses the eviction victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The new object that needs to be inserted into the cache.
    - Return:
        - `candid_obj_key`: The key of the cached object that will be evicted to make room for `obj`.
    '''
    global m_probation, m_protected, m_b1, m_b2, m_freq, m_p
    _reset_if_new_run(cache_snapshot)
    _prune_metadata(cache_snapshot)

    # Seed metadata if empty by placing all current cache keys into probation.
    if not m_probation and not m_protected and cache_snapshot.cache:
        for k0 in cache_snapshot.cache.keys():
            m_probation[k0] = None

    # ARC: adjust target m_p based on ghost hit of incoming key
    cap = max(int(cache_snapshot.capacity), 1)
    k = obj.key
    if k in m_b1:
        delta = max(1, len(m_b2) // max(1, len(m_b1)))
        m_p = min(cap, m_p + delta)
    elif k in m_b2:
        delta = max(1, len(m_b1) // max(1, len(m_b2)))
        m_p = max(0, m_p - delta)

    # ARC replacement decision (do not mutate structures here)
    candid_obj_key = None
    if m_probation and ((k in m_b2 and len(m_probation) == m_p) or (len(m_probation) > m_p)):
        # Evict LRU from probation (T1)
        candid_obj_key = next(iter(m_probation))
    elif m_protected:
        # Evict LRU from protected (T2)
        candid_obj_key = next(iter(m_protected))
    elif m_probation:
        candid_obj_key = next(iter(m_probation))
    else:
        # Fallback: choose any key from the cache
        for k_any in cache_snapshot.cache.keys():
            candid_obj_key = k_any
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
    global m_probation, m_protected, m_b1, m_b2, m_freq
    _reset_if_new_run(cache_snapshot)
    _prune_metadata(cache_snapshot)

    k = obj.key
    # Bump lightweight frequency (not central to ARC but harmless)
    m_freq[k] = m_freq.get(k, 0) + 1

    if k in m_protected:
        # Refresh recency in protected
        m_protected.move_to_end(k, last=True)
    elif k in m_probation:
        # Promote to protected on hit
        m_probation.pop(k, None)
        m_protected[k] = None
    else:
        # Metadata miss but cache hit: treat as hot and place into protected
        m_protected[k] = None


def update_after_insert(cache_snapshot, obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after inserting a new object into the cache.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object that was just inserted into the cache.
    - Return: `None`
    '''
    global m_probation, m_protected, m_b1, m_b2, m_freq
    _reset_if_new_run(cache_snapshot)
    _prune_metadata(cache_snapshot)

    k = obj.key
    # Remove any stale placements
    m_protected.pop(k, None)
    m_probation.pop(k, None)

    # ARC admission: if key was in ghosts, insert to protected (T2); else to probation (T1)
    if k in m_b1:
        m_b1.pop(k, None)
        m_protected[k] = None
    elif k in m_b2:
        m_b2.pop(k, None)
        m_protected[k] = None
    else:
        m_probation[k] = None

    # Light frequency credit
    m_freq[k] = m_freq.get(k, 0) + 1

    # Bound ghost sizes to capacity
    cap = max(int(cache_snapshot.capacity), 1)
    while len(m_b1) > cap:
        m_b1.popitem(last=False)
    while len(m_b2) > cap:
        m_b2.popitem(last=False)


def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after evicting the victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object to be inserted into the cache.
        - `evicted_obj`: The object that was just evicted from the cache.
    - Return: `None`
    '''
    global m_probation, m_protected, m_b1, m_b2
    _reset_if_new_run(cache_snapshot)
    # Move evicted object into appropriate ghost list
    k = evicted_obj.key
    if k in m_probation:
        m_probation.pop(k, None)
        m_b1.pop(k, None)
        m_b1[k] = None  # MRU of B1
    elif k in m_protected:
        m_protected.pop(k, None)
        m_b2.pop(k, None)
        m_b2[k] = None  # MRU of B2
    else:
        # No segment info; do nothing
        pass

    # Bound ghost sizes to capacity
    cap = max(int(cache_snapshot.capacity), 1)
    while len(m_b1) > cap:
        m_b1.popitem(last=False)
    while len(m_b2) > cap:
        m_b2.popitem(last=False)

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