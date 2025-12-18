# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

# ARC (Adaptive Replacement Cache) Global State
m_lru = {}    # key -> access_count (timestamp)
m_t1 = set()  # T1: Recent set (keys)
m_t2 = set()  # T2: Frequent set (keys)
m_b1 = set()  # B1: Ghost Recent set (keys)
m_b2 = set()  # B2: Ghost Frequent set (keys)
m_p = 0.0     # Adaptation parameter (target size of T1)
m_last_access_count = 0 # To detect trace resets

def check_reset(cache_snapshot):
    global m_last_access_count, m_lru, m_t1, m_t2, m_b1, m_b2, m_p
    current_count = cache_snapshot.access_count
    if current_count < m_last_access_count:
        # Trace reset detected
        m_lru.clear()
        m_t1.clear()
        m_t2.clear()
        m_b1.clear()
        m_b2.clear()
        m_p = 0.0
    m_last_access_count = current_count

def evict(cache_snapshot, obj):
    '''
    Choose eviction victim using ARC logic.
    '''
    check_reset(cache_snapshot)
    global m_p, m_t1, m_t2, m_b1, m_b2, m_lru

    # Adaptation of p
    if obj.key in m_b1:
        delta = 1.0
        if len(m_b1) < len(m_b2):
            delta = float(len(m_b2)) / len(m_b1)
        m_p = min(float(cache_snapshot.capacity), m_p + delta)
    elif obj.key in m_b2:
        delta = 1.0
        if len(m_b2) < len(m_b1):
            delta = float(len(m_b1)) / len(m_b2)
        m_p = max(0.0, m_p - delta)

    # Determine victim
    victim_key = None

    # ARC Replace Logic
    # We rely on m_t1 and m_t2 tracking the keys in cache.
    # We must filter by actual cache content to be safe against state drift or initialization.
    t1_candidates = [k for k in m_t1 if k in cache_snapshot.cache]
    t2_candidates = [k for k in m_t2 if k in cache_snapshot.cache]

    # Fallback if sets are empty but cache is not (should not happen if consistent)
    if not t1_candidates and not t2_candidates:
        # Use full cache as fallback
        t1_candidates = list(cache_snapshot.cache.keys())

    evict_from_t1 = False
    if len(t1_candidates) > 0:
        if len(t1_candidates) > m_p:
            evict_from_t1 = True
        elif (obj.key in m_b2) and (len(t1_candidates) == int(m_p)):
            evict_from_t1 = True

    # If T1 is chosen, or if T2 is empty, evict from T1
    if (evict_from_t1 or not t2_candidates) and t1_candidates:
        victim_key = min(t1_candidates, key=lambda k: m_lru.get(k, 0))
    else:
        # Otherwise evict from T2
        victim_key = min(t2_candidates, key=lambda k: m_lru.get(k, 0))

    return victim_key

def update_after_hit(cache_snapshot, obj):
    '''
    Update metadata after hit. Move to T2 (Frequent).
    '''
    check_reset(cache_snapshot)
    global m_lru, m_t1, m_t2
    m_lru[obj.key] = cache_snapshot.access_count

    if obj.key in m_t1:
        m_t1.remove(obj.key)
        m_t2.add(obj.key)
    # If in T2, it stays in T2. LRU updated.

def update_after_insert(cache_snapshot, obj):
    '''
    Update metadata after insert. Handle ghost hits.
    '''
    check_reset(cache_snapshot)
    global m_lru, m_t1, m_t2, m_b1, m_b2
    m_lru[obj.key] = cache_snapshot.access_count

    # Ghost hits promote to T2
    if obj.key in m_b1:
        m_b1.remove(obj.key)
        m_t2.add(obj.key)
    elif obj.key in m_b2:
        m_b2.remove(obj.key)
        m_t2.add(obj.key)
    else:
        # New object -> T1
        m_t1.add(obj.key)
        # Safety: ensure not in T2
        if obj.key in m_t2:
            m_t2.remove(obj.key)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Update metadata after eviction. Move to ghosts.
    '''
    check_reset(cache_snapshot)
    global m_t1, m_t2, m_b1, m_b2, m_lru

    if evicted_obj.key in m_t1:
        m_t1.remove(evicted_obj.key)
        m_b1.add(evicted_obj.key)
    elif evicted_obj.key in m_t2:
        m_t2.remove(evicted_obj.key)
        m_b2.add(evicted_obj.key)

    # Manage ghost size (limit total ghosts to capacity)
    target_ghost_size = cache_snapshot.capacity
    if len(m_b1) + len(m_b2) > target_ghost_size:
        ghost_keys = list(m_b1) + list(m_b2)
        if ghost_keys:
            victim_ghost = min(ghost_keys, key=lambda k: m_lru.get(k, 0))
            if victim_ghost in m_b1:
                m_b1.remove(victim_ghost)
            elif victim_ghost in m_b2:
                m_b2.remove(victim_ghost)
            if victim_ghost in m_lru:
                del m_lru[victim_ghost]

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