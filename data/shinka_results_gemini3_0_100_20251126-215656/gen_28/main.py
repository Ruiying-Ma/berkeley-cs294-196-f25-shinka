# EVOLVE-BLOCK-START
"""
Adaptive S3-FIFO v2 (AS3FIFO_v2)
An evolution of Adaptive S3-FIFO incorporating larger ghost registries and robust trace detection.
Optimized for minimizing miss rate by dynamically balancing S (Probationary) and M (Protected) queues.
"""

from collections import OrderedDict

# Global State
s_queue = OrderedDict()    # Small/Probationary FIFO
m_queue = OrderedDict()    # Main/Protected FIFO
ghost_s = OrderedDict()    # Ghost registry for S (evicted from S)
ghost_m = OrderedDict()    # Ghost registry for M (evicted from M)
accessed_bits = set()      # Tracks access status (approximate LRU/Frequency)
s_dist = 0.1               # Target fraction for S queue (adaptive)
last_access_count = 0      # To detect trace resets

def check_reset(cache_snapshot):
    """
    Resets internal state if a new trace is detected.
    Checks for access count rollback or empty cache with residual state.
    """
    global s_queue, m_queue, ghost_s, ghost_m, accessed_bits, s_dist, last_access_count
    
    current_acc = cache_snapshot.access_count
    
    # Detect trace restart or context switch
    if current_acc < last_access_count or (len(cache_snapshot.cache) <= 1 and (len(s_queue) > 1 or len(m_queue) > 1)):
        s_queue.clear()
        m_queue.clear()
        ghost_s.clear()
        ghost_m.clear()
        accessed_bits.clear()
        s_dist = 0.1
        last_access_count = 0
    else:
        last_access_count = current_acc

def evict(cache_snapshot, obj):
    '''
    Selects a victim using Adaptive S3-FIFO logic.
    Dynamically balances between S and M queues based on s_dist.
    '''
    global s_dist
    
    capacity = cache_snapshot.capacity
    # Target size for S based on adaptive distribution
    s_target = max(1, int(capacity * s_dist))
    # Ghost limit 2x capacity to catch longer loops (ARC-style)
    ghost_limit = capacity * 2
    
    while True:
        # Determine eviction candidate source
        # Evict from S if it's too big or if M is empty
        evict_s = (len(s_queue) > s_target) or (len(m_queue) == 0)
        
        if evict_s:
            if not s_queue:
                # Fallback: if logic says S but S is empty, try M
                if m_queue:
                    evict_s = False
                else:
                    # Emergency: cache inconsistent or empty
                    return next(iter(cache_snapshot.cache))
            
            if evict_s:
                key, _ = s_queue.popitem(last=False) # Head of S
                
                if key in accessed_bits:
                    # Second Chance: Promote to M
                    accessed_bits.discard(key)
                    m_queue[key] = None
                    # Continue loop to find actual victim
                else:
                    # Victim found in S
                    ghost_s[key] = None
                    if len(ghost_s) > ghost_limit:
                        ghost_s.popitem(last=False)
                    return key

        if not evict_s:
            if not m_queue:
                return next(iter(cache_snapshot.cache))
            
            key, _ = m_queue.popitem(last=False) # Head of M
            
            if key in accessed_bits:
                # Second Chance: Reinsert to M tail
                accessed_bits.discard(key)
                m_queue[key] = None
            else:
                # Victim found in M
                ghost_m[key] = None
                if len(ghost_m) > ghost_limit:
                    ghost_m.popitem(last=False)
                return key

def update_after_hit(cache_snapshot, obj):
    check_reset(cache_snapshot)
    accessed_bits.add(obj.key)

def update_after_insert(cache_snapshot, obj):
    check_reset(cache_snapshot)
    
    global s_dist
    key = obj.key
    capacity = cache_snapshot.capacity
    
    # Adaptive sizing delta (ARC-style)
    delta = 1.0 / capacity if capacity > 0 else 0.01
    
    if key in ghost_s:
        # Hit in Ghost S: S was too small. Increase S target.
        s_dist = min(0.9, s_dist + delta)
        # Rescue to M
        m_queue[key] = None
        del ghost_s[key]
        accessed_bits.discard(key)
        
    elif key in ghost_m:
        # Hit in Ghost M: M was too small. Decrease S target.
        s_dist = max(0.01, s_dist - delta)
        # Rescue to M
        m_queue[key] = None
        del ghost_m[key]
        accessed_bits.discard(key)
        
    else:
        # New insert: enter S
        s_queue[key] = None
        accessed_bits.discard(key)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    # Ensure internal state consistency
    key = evicted_obj.key
    accessed_bits.discard(key)
    if key in s_queue:
        del s_queue[key]
    if key in m_queue:
        del m_queue[key]
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