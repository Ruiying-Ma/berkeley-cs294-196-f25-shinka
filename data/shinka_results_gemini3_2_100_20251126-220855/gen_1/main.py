# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

m_key_timestamp = dict()
m_protected_keys = set()
m_ghost_timestamp = dict()

def evict(cache_snapshot, obj):
    '''
    This function defines how the algorithm chooses the eviction victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The new object that needs to be inserted into the cache.
    - Return:
        - `candid_obj_key`: The key of the cached object that will be evicted to make room for `obj`.
    '''
    global m_key_timestamp, m_protected_keys

    # Calculate current size of A1in (items in cache but not protected)
    a1in_keys = [k for k in cache_snapshot.cache if k not in m_protected_keys]
    current_a1in_size = len(a1in_keys)

    # Target size for A1in (Probation)
    target_a1in = cache_snapshot.capacity * 0.25

    victim_key = None
    candidates = []

    # Decide whether to evict from A1in or Am (Protected)
    # If A1in exceeds target, evict from A1in.
    # Also if Am is empty (all items are probationary), must evict from A1in.
    if current_a1in_size > target_a1in or current_a1in_size == len(cache_snapshot.cache):
        candidates = a1in_keys
    else:
        # Otherwise try to evict from Am
        candidates = [k for k in cache_snapshot.cache if k in m_protected_keys]
        # Fallback if candidates is empty (should be covered by condition above)
        if not candidates:
            candidates = a1in_keys

    # LRU eviction within the chosen segment
    victim_key = min(candidates, key=lambda k: m_key_timestamp.get(k, 0))
    return victim_key

def update_after_hit(cache_snapshot, obj):
    '''
    This function defines how the algorithm update the metadata it maintains immediately after a cache hit.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object accessed during the cache hit.
    - Return: `None`
    '''
    global m_key_timestamp
    # Update access time (LRU)
    # In 2Q, hits in A1in do NOT promote to Am. They just refresh position in A1in.
    if obj.key in m_key_timestamp:
        m_key_timestamp[obj.key] = cache_snapshot.access_count

def update_after_insert(cache_snapshot, obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after inserting a new object into the cache.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object that was just inserted into the cache.
    - Return: `None`
    '''
    global m_key_timestamp, m_protected_keys, m_ghost_timestamp
    m_key_timestamp[obj.key] = cache_snapshot.access_count

    # Check if object was in ghost registry
    if obj.key in m_ghost_timestamp:
        # Promote to Protected (Am)
        m_protected_keys.add(obj.key)
        del m_ghost_timestamp[obj.key]
    else:
        # Insert into Probation (A1in)
        # Ensure it's not marked as protected (in case of re-use of key after long time)
        if obj.key in m_protected_keys:
            m_protected_keys.remove(obj.key)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after evicting the victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object to be inserted into the cache.
        - `evicted_obj`: The object that was just evicted from the cache.
    - Return: `None`
    '''
    global m_key_timestamp, m_protected_keys, m_ghost_timestamp
    if evicted_obj.key in m_key_timestamp:
        del m_key_timestamp[evicted_obj.key]

    if evicted_obj.key in m_protected_keys:
        m_protected_keys.remove(evicted_obj.key)
        # Evicted from Am: usually not added to ghost in standard 2Q
    else:
        # Evicted from A1in: add to ghost registry
        m_ghost_timestamp[evicted_obj.key] = cache_snapshot.access_count

        # Manage ghost size (limit to cache capacity)
        if len(m_ghost_timestamp) > cache_snapshot.capacity:
             # Remove oldest in ghost
             oldest_ghost = min(m_ghost_timestamp, key=m_ghost_timestamp.get)
             del m_ghost_timestamp[oldest_ghost]

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