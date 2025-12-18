# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

# S3-FIFO-D Globals
m_small = dict()
m_main = dict()
m_ghost = dict()
m_hits = dict()

def evict(cache_snapshot, obj):
    '''
    S3-FIFO-D Eviction Logic:
    - New items enter Small.
    - Eviction prioritizes dropping from Small (if > 10% cap) or Main.
    - Items in Small with hits promote to Main.
    - Items in Main with hits get second chance (reinserted).
    '''
    global m_small, m_main, m_hits

    capacity = cache_snapshot.capacity
    # Small queue size target: 10% of capacity
    s_capacity = max(1, int(capacity * 0.1))

    while True:
        # If Small is larger than target, or Main is empty, we evict from Small
        if len(m_small) > s_capacity or len(m_main) == 0:
            if not m_small:
                # Should not happen if cache is full and Main is empty
                # If Main has items, pick from Main
                if m_main:
                    return next(iter(m_main))
                return next(iter(cache_snapshot.cache))

            candidate = next(iter(m_small))
            if m_hits.get(candidate, 0) > 0:
                # Hit in Small -> Promote to Main
                m_hits[candidate] = 0
                del m_small[candidate]
                m_main[candidate] = None
            else:
                # No hit -> Evict from Small
                return candidate
        else:
            # Evict from Main
            if not m_main:
                # Fallback to Small
                return next(iter(m_small))

            candidate = next(iter(m_main))
            if m_hits.get(candidate, 0) > 0:
                # Hit in Main -> Reinsert at tail (Second Chance)
                m_hits[candidate] = 0
                del m_main[candidate]
                m_main[candidate] = None
            else:
                # No hit -> Evict from Main
                return candidate

def update_after_hit(cache_snapshot, obj):
    '''
    On Cache Hit:
    - Increment hit counter (saturated at 3).
    '''
    global m_hits
    m_hits[obj.key] = min(m_hits.get(obj.key, 0) + 1, 3)

def update_after_insert(cache_snapshot, obj):
    '''
    On Cache Insert (Miss):
    - Reset state if new trace.
    - Insert into Small or Main (if in Ghost).
    '''
    global m_small, m_main, m_hits, m_ghost

    if cache_snapshot.access_count <= 1:
        m_small.clear()
        m_main.clear()
        m_hits.clear()
        m_ghost.clear()

    # S3-FIFO-D: If in ghost, insert to Main (rescue). Else Small.
    if obj.key in m_ghost:
        del m_ghost[obj.key]
        m_main[obj.key] = None
    else:
        m_small[obj.key] = None

    m_hits[obj.key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    After Eviction:
    - Manage Ghost list for items evicted from Small.
    - Cleanup data structures.
    '''
    global m_small, m_main, m_hits, m_ghost

    key = evicted_obj.key

    if key in m_small:
        # Evicted from Small without promotion -> Add to Ghost
        del m_small[key]
        m_ghost[key] = None
    elif key in m_main:
        del m_main[key]

    if key in m_hits:
        del m_hits[key]

    # Limit Ghost size to Cache Capacity
    while len(m_ghost) > cache_snapshot.capacity:
        m_ghost.pop(next(iter(m_ghost)))

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