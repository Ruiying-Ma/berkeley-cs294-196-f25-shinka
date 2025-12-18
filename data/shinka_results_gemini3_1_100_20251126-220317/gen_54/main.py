# EVOLVE-BLOCK-START
"""Cache eviction algorithm combining S3-FIFO structure with ARC-like adaptation"""

# Globals
m_small = dict()
m_main = dict()
g_small = dict()
g_main = dict()
m_hits = dict()
s_ratio = 0.1

def evict(cache_snapshot, obj):
    '''
    Adaptive S3-FIFO Eviction:
    - Main and Small queues.
    - Adaptive Small queue size ratio (s_ratio) based on ghost hits.
    - Second-chance mechanism for both queues using hit counts.
    '''
    global m_small, m_main, m_hits, s_ratio

    capacity = cache_snapshot.capacity
    s_target = max(1, int(capacity * s_ratio))

    while True:
        # Check if we should evict from Small
        # Condition: Small is over target size OR Main is empty (must evict from Small)
        if len(m_small) > s_target or not m_main:
            if not m_small:
                # Fallback: if Small is empty but Main is not (shouldn't reach here due to 'or not m_main' but for safety)
                if m_main:
                    candidate = next(iter(m_main))
                else:
                    return next(iter(cache_snapshot.cache))
            else:
                candidate = next(iter(m_small))

            if m_hits.get(candidate, 0) > 0:
                # Hit in Small -> Promote to Main
                m_hits[candidate] = 0
                del m_small[candidate]
                m_main[candidate] = None
                # Loop continues to find next victim
            else:
                # No hit -> Evict from Small
                return candidate
        else:
            # Evict from Main
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
    - Adapt s_ratio based on ghost hits.
    - Insert into Small or Main.
    '''
    global m_small, m_main, m_hits, g_small, g_main, s_ratio

    # Reset state if new trace
    if cache_snapshot.access_count <= 1:
        m_small.clear()
        m_main.clear()
        m_hits.clear()
        g_small.clear()
        g_main.clear()
        s_ratio = 0.1

    key = obj.key
    capacity = cache_snapshot.capacity
    
    # Check ghost hits to adapt s_ratio
    if key in g_small:
        # Hit in ghost small -> Small was too small
        delta = 1.0
        if len(g_main) > 0:
            delta = len(g_main) / len(g_small)
        delta_ratio = delta / capacity if capacity > 0 else 0.1
        
        s_ratio = min(0.99, s_ratio + delta_ratio)
        
        del g_small[key]
        m_main[key] = None # Rescue to Main
        m_hits[key] = 0
        
    elif key in g_main:
        # Hit in ghost main -> Main was too small
        delta = 1.0
        if len(g_small) > 0:
            delta = len(g_small) / len(g_main)
        delta_ratio = delta / capacity if capacity > 0 else 0.1
        
        s_ratio = max(0.01, s_ratio - delta_ratio)
        
        del g_main[key]
        m_main[key] = None # Rescue to Main
        m_hits[key] = 0
        
    else:
        # Completely new item
        m_small[key] = None
        m_hits[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    After Eviction:
    - Add to appropriate ghost list.
    '''
    global m_small, m_main, m_hits, g_small, g_main

    key = evicted_obj.key

    # Identify where it was evicted from and move to ghost
    if key in m_small:
        del m_small[key]
        g_small[key] = None
    elif key in m_main:
        del m_main[key]
        g_main[key] = None

    if key in m_hits:
        del m_hits[key]

    # Limit ghost size
    # Maintain total ghost size <= capacity (ARC style)
    capacity = cache_snapshot.capacity
    while len(g_small) + len(g_main) > capacity:
        if len(g_small) > len(g_main):
            if g_small:
                g_small.pop(next(iter(g_small)))
            elif g_main:
                 g_main.pop(next(iter(g_main)))
        else:
            if g_main:
                g_main.pop(next(iter(g_main)))
            elif g_small:
                g_small.pop(next(iter(g_small)))
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