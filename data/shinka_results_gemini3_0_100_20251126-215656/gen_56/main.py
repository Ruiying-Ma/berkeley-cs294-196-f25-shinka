# EVOLVE-BLOCK-START
"""
Adaptive S3-FIFO with Conditional Demotion (AS3-CD)
Combines the adaptive sizing of ARC/A-S3-FIFO with the conditional demotion strategy.
- S/M Queues: Partitioned cache.
- Adaptive Sizing: s_dist adjusts based on ghost hits to find optimal S/M split.
- Conditional Demotion: M-victims are demoted to S (giving a second chance) only if S is under its target size. 
  Otherwise, they are evicted to Ghost M.
- Ghost S: Tracks items evicted from S. Hits -> Grow S.
- Ghost M: Tracks items evicted from M (when S was full). Hits -> Grow M (Shrink S).
"""

from collections import OrderedDict

# Global state
s_queue = OrderedDict()    # Small/Probationary FIFO
m_queue = OrderedDict()    # Main/Protected FIFO
ghost_s = OrderedDict()    # Ghost registry for S
ghost_m = OrderedDict()    # Ghost registry for M
accessed_bits = set()      # Tracks access status
s_dist = 0.1               # Target fraction for S queue (0.0 to 1.0)

def reset_globals_if_new_trace(cache_snapshot):
    """
    Reset globals if a new trace is detected.
    """
    global s_queue, m_queue, ghost_s, ghost_m, accessed_bits, s_dist
    if len(cache_snapshot.cache) <= 1 and (len(s_queue) > 1 or len(m_queue) > 1):
        s_queue.clear()
        m_queue.clear()
        ghost_s.clear()
        ghost_m.clear()
        accessed_bits.clear()
        s_dist = 0.1

def evict(cache_snapshot, obj):
    '''
    Adaptive S3-FIFO eviction with Conditional Demotion.
    '''
    global s_dist
    
    capacity = cache_snapshot.capacity
    # Target size for S based on adaptive distribution
    s_target = max(1, int(capacity * s_dist))
    
    # Safety: if queues empty but cache full
    if not s_queue and not m_queue:
         return next(iter(cache_snapshot.cache))

    while True:
        # Determine if we should evict from S or M
        # Evict from S if it exceeds its target share, or if M is empty
        evict_s = (len(s_queue) > s_target) or (len(m_queue) == 0)
        
        if evict_s:
            if not s_queue:
                # Fallback (rare consistency fix)
                if m_queue:
                    key, _ = m_queue.popitem(last=False)
                    return key
                return next(iter(cache_snapshot.cache))
            
            key, _ = s_queue.popitem(last=False) # FIFO head
            
            if key in accessed_bits:
                # Hit in S -> Promote to M (Pass probation)
                accessed_bits.discard(key)
                m_queue[key] = None
            else:
                # Evict from S -> Ghost S
                ghost_s[key] = None
                if len(ghost_s) > capacity:
                    ghost_s.popitem(last=False)
                return key
        else:
            # Evict from M
            key, _ = m_queue.popitem(last=False) # FIFO head
            
            if key in accessed_bits:
                # Second chance: reinsert to M tail
                accessed_bits.discard(key)
                m_queue[key] = None
            else:
                # M victim found.
                # Conditional Demotion:
                # If S has capacity, give this item a chance in S (Probation)
                if len(s_queue) < s_target:
                    s_queue[key] = None
                    # Loop continues; we moved item internaly, didn't evict from cache yet.
                else:
                    # S is full, evict from M -> Ghost M
                    ghost_m[key] = None
                    if len(ghost_m) > capacity:
                        ghost_m.popitem(last=False)
                    return key

def update_after_hit(cache_snapshot, obj):
    accessed_bits.add(obj.key)

def update_after_insert(cache_snapshot, obj):
    reset_globals_if_new_trace(cache_snapshot)
    
    global s_dist
    key = obj.key
    capacity = cache_snapshot.capacity
    
    # Calculate adaptation step size
    delta = 1.0 / capacity if capacity > 0 else 0.01
    
    if key in ghost_s:
        # Hit in Ghost S: S was too small.
        # Grow S
        s_dist = min(0.9, s_dist + delta)
        # Rescue to M (Assume it belongs in working set)
        m_queue[key] = None
        del ghost_s[key]
        
    elif key in ghost_m:
        # Hit in Ghost M: M was too small (S too big/clogged).
        # Shrink S (Grow M)
        s_dist = max(0.01, s_dist - delta)
        # Rescue to M
        m_queue[key] = None
        del ghost_m[key]
        
    else:
        # New item -> S
        s_queue[key] = None
    
    # Reset access bit for new/promoted item
    accessed_bits.discard(key)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    accessed_bits.discard(evicted_obj.key)
    # Cleanup ensures we don't hold references to evicted items
    if evicted_obj.key in s_queue:
        del s_queue[evicted_obj.key]
    if evicted_obj.key in m_queue:
        del m_queue[evicted_obj.key]
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