# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

# ARC Global State
m_lru = {}    # key -> access_count
m_t1 = set()  # T1 keys (Recent)
m_t2 = set()  # T2 keys (Frequent)
m_b1 = set()  # B1 keys (Ghost Recent)
m_b2 = set()  # B2 keys (Ghost Frequent)
m_p = 0.0     # Adaptation parameter (Target T1 size)
m_last_access_count = 0

def check_reset(cache_snapshot):
    global m_last_access_count, m_lru, m_t1, m_t2, m_b1, m_b2, m_p
    current_count = cache_snapshot.access_count
    if current_count < m_last_access_count:
        m_lru.clear()
        m_t1.clear()
        m_t2.clear()
        m_b1.clear()
        m_b2.clear()
        m_p = 0.0
    m_last_access_count = current_count

def evict(cache_snapshot, obj):
    '''
    This function defines how the algorithm chooses the eviction victim.
    '''
    check_reset(cache_snapshot)
    global m_p, m_t1, m_t2, m_b1, m_b2, m_lru

    # ARC Adaptation: Adjust p based on ghost hits
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

    # Filter sets to match actual cache (handle drift/init)
    t1_real = [k for k in m_t1 if k in cache_snapshot.cache]
    t2_real = [k for k in m_t2 if k in cache_snapshot.cache]

    # Fallback
    if not t1_real and not t2_real:
        t1_real = list(cache_snapshot.cache.keys())

    # ARC Replace Logic
    replace_t1 = False
    if len(t1_real) > 0 and len(t1_real) > m_p:
        replace_t1 = True
    elif len(t1_real) > 0 and (obj.key in m_b2) and (len(t1_real) == int(m_p)):
        replace_t1 = True

    victim_key = None
    if (replace_t1 or not t2_real) and t1_real:
        victim_key = min(t1_real, key=lambda k: m_lru.get(k, 0))
    else:
        # Evict from T2
        victim_key = min(t2_real, key=lambda k: m_lru.get(k, 0))

    return victim_key

def update_after_hit(cache_snapshot, obj):
    '''
    Update metadata after hit. Move to T2.
    '''
    check_reset(cache_snapshot)
    global m_lru, m_t1, m_t2, m_b1, m_b2
    m_lru[obj.key] = cache_snapshot.access_count

    if obj.key in m_t1:
        m_t1.remove(obj.key)
        m_t2.add(obj.key)
    # If already in T2, stays in T2.

def update_after_insert(cache_snapshot, obj):
    '''
    Update metadata after insert. Handle ghost hits.
    '''
    check_reset(cache_snapshot)
    global m_lru, m_t1, m_t2, m_b1, m_b2
    m_lru[obj.key] = cache_snapshot.access_count

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

    # Manage ghost size: limit total ghosts to capacity
    target_ghost = cache_snapshot.capacity
    if len(m_b1) + len(m_b2) > target_ghost:
        # Evict LRU from ghosts (B1 U B2)
        # Optimization: Scan only if needed.
        # Simple implementation: check all ghosts.
        # Since ghosts are not in cache, we use m_lru timestamps.
        # But iterating all ghosts might be O(N). N=capacity. Accepted.
        victim_ghost = None
        min_ts = float('inf')

        # Iterate B1
        for k in m_b1:
            ts = m_lru.get(k, 0)
            if ts < min_ts:
                min_ts = ts
                victim_ghost = k
        # Iterate B2
        for k in m_b2:
            ts = m_lru.get(k, 0)
            if ts < min_ts:
                min_ts = ts
                victim_ghost = k

        if victim_ghost:
            if victim_ghost in m_b1: m_b1.remove(victim_ghost)
            elif victim_ghost in m_b2: m_b2.remove(victim_ghost)
            if victim_ghost in m_lru: del m_lru[victim_ghost]

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