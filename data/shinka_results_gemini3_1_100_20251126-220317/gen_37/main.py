# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

# S3-FIFO-F (Frequency enhanced) Globals
m_small = dict()
m_main = dict()
m_ghost_s = dict()
m_ghost_m = dict()
m_freq = dict()

def evict(cache_snapshot, obj):
    '''
    S3-FIFO-F Eviction:
    - 10% Small, 90% Main.
    - Uses frequency counters (0-3) for aging in Main instead of a binary second-chance.
    - Promotes from Small to Main if freq > 0.
    '''
    global m_small, m_main, m_freq
    
    capacity = cache_snapshot.capacity
    target_small = max(1, int(capacity * 0.1))
    
    # Safety loop limit to prevent infinite reinsertions (though freq decrement guarantees termination)
    # Worst case: All items have max freq (3). We iterate 3 * N times.
    limit = (len(m_small) + len(m_main)) * 4 + 10
    
    while limit > 0:
        limit -= 1
        
        # Determine which queue to evict from
        evict_small = False
        if len(m_small) > target_small:
            evict_small = True
        elif not m_main:
            evict_small = True
            
        if evict_small:
            if not m_small:
                # Should not happen if cache is full
                if m_main: return next(iter(m_main))
                return next(iter(cache_snapshot.cache))
                
            candidate = next(iter(m_small))
            freq = m_freq.get(candidate, 0)
            
            if freq > 0:
                # Promote to Main
                del m_small[candidate]
                m_main[candidate] = None
                # Cap freq for promoted item (2 gives it decent survival chance in Main)
                m_freq[candidate] = min(freq, 2)
            else:
                # Victim found in Small
                return candidate
        else:
            # Check Main
            if not m_main:
                # Fallback
                return next(iter(m_small))
                
            candidate = next(iter(m_main))
            freq = m_freq.get(candidate, 0)
            
            if freq > 0:
                # Reinsert / Aging
                del m_main[candidate]
                m_main[candidate] = None # Move to tail
                m_freq[candidate] = freq - 1 # Decrement frequency (Aging)
            else:
                # Victim found in Main
                return candidate
    
    # Emergency fallback
    if m_small: return next(iter(m_small))
    return next(iter(m_main))

def update_after_hit(cache_snapshot, obj):
    '''
    On Hit: Increment frequency, saturated at 3.
    '''
    global m_freq
    m_freq[obj.key] = min(m_freq.get(obj.key, 0) + 1, 3)

def update_after_insert(cache_snapshot, obj):
    '''
    On Insert:
    - Reset if new trace.
    - Insert to Main if in Ghost (restoring some freq).
    - Else Insert to Small.
    '''
    global m_small, m_main, m_ghost_s, m_ghost_m, m_freq

    if cache_snapshot.access_count <= 1:
        m_small.clear()
        m_main.clear()
        m_ghost_s.clear()
        m_ghost_m.clear()
        m_freq.clear()
        
    key = obj.key
    # Default initial frequency
    m_freq[key] = 0
    
    if key in m_ghost_m:
        # Rescuing from Main Ghost -> Main
        del m_ghost_m[key]
        m_main[key] = None
        m_freq[key] = 1 # Restore warmth (1 life)
    elif key in m_ghost_s:
        # Rescuing from Small Ghost -> Main
        del m_ghost_s[key]
        m_main[key] = None
        m_freq[key] = 0 # Probation in Main
    else:
        # New -> Small
        m_small[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    After Eviction:
    - Move to appropriate Ghost list.
    - Cleanup frequency map for fully evicted items.
    '''
    global m_small, m_main, m_ghost_s, m_ghost_m, m_freq
    
    key = evicted_obj.key
    capacity = cache_snapshot.capacity
    
    # Identify source queue (victim was not removed in evict, only returned)
    if key in m_small:
        del m_small[key]
        m_ghost_s[key] = None
    elif key in m_main:
        del m_main[key]
        m_ghost_m[key] = None
        
    # Enforce Ghost Capacities and cleanup associated frequency data
    while len(m_ghost_s) > capacity:
        k = next(iter(m_ghost_s))
        del m_ghost_s[k]
        if k in m_freq: del m_freq[k]
        
    while len(m_ghost_m) > capacity:
        k = next(iter(m_ghost_m))
        del m_ghost_m[k]
        if k in m_freq: del m_freq[k]
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