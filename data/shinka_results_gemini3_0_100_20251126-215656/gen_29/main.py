# EVOLVE-BLOCK-START
from collections import OrderedDict

# Global State
s_queue = OrderedDict()      # Small/Probationary FIFO
m_queue = OrderedDict()      # Main/Protected FIFO
ghost_s = OrderedDict()      # Ghost S (Evicted from S, originating in S)
ghost_m = OrderedDict()      # Ghost M (Evicted from M -> S -> Evicted)
accessed_bits = set()        # Access bits
demoted_from_m = set()       # Tracks items demoted from M to S
s_dist = 0.1                 # Target S-queue fraction
last_access_count = 0        # Trace reset detection

def check_reset(cache_snapshot):
    """Detects new trace and resets globals."""
    global s_queue, m_queue, ghost_s, ghost_m, accessed_bits, demoted_from_m, s_dist, last_access_count
    
    current_acc = cache_snapshot.access_count
    if current_acc < last_access_count or (len(cache_snapshot.cache) <= 1 and len(s_queue) > 1):
        s_queue.clear()
        m_queue.clear()
        ghost_s.clear()
        ghost_m.clear()
        accessed_bits.clear()
        demoted_from_m.clear()
        s_dist = 0.1
        last_access_count = 0
    else:
        last_access_count = current_acc

def evict(cache_snapshot, obj):
    '''
    Selects a victim using Hyper S3-FIFO.
    Uses Demotion (M->S) and Source-Aware Ghosts.
    '''
    global s_dist
    
    capacity = cache_snapshot.capacity
    s_target = max(1, int(capacity * s_dist))
    ghost_limit = capacity * 2
    
    while True:
        # Decision: Evict from S if it's too big or M is empty
        # Note: If we just demoted M->S, S grew, so we likely check S next.
        evict_s = (len(s_queue) > s_target) or (len(m_queue) == 0)
        
        if evict_s:
            if not s_queue:
                # Should not happen if logic holds, fallback
                if m_queue:
                    evict_s = False
                else:
                    return next(iter(cache_snapshot.cache))
            
            if evict_s:
                key, _ = s_queue.popitem(last=False) # Head of S
                
                if key in accessed_bits:
                    # Second Chance: Promote to M
                    accessed_bits.discard(key)
                    m_queue[key] = None
                    if key in demoted_from_m:
                        demoted_from_m.discard(key)
                else:
                    # Victim found in S
                    # Check origin to decide which Ghost to use
                    if key in demoted_from_m:
                        # It was from M, demoted, now evicted. 
                        # Feedback: M was too small.
                        ghost_m[key] = None
                        if len(ghost_m) > ghost_limit:
                            ghost_m.popitem(last=False)
                        demoted_from_m.discard(key)
                    else:
                        # It was from S (new item), evicted.
                        # Feedback: S was too small.
                        ghost_s[key] = None
                        if len(ghost_s) > ghost_limit:
                            ghost_s.popitem(last=False)
                    return key

        if not evict_s:
            if not m_queue:
                return next(iter(cache_snapshot.cache))
            
            key, _ = m_queue.popitem(last=False) # Head of M
            
            if key in accessed_bits:
                # Second Chance in M: Reinsert to M tail
                accessed_bits.discard(key)
                m_queue[key] = None
            else:
                # Demotion: M -> S
                # Give it a last chance in S (compete with new items)
                # This ensures we don't evict potentially warm items too fast compared to cold new items
                s_queue[key] = None
                demoted_from_m.add(key)
                # Loop continues (now S is bigger, likely will trigger evict_s next)

def update_after_hit(cache_snapshot, obj):
    check_reset(cache_snapshot)
    accessed_bits.add(obj.key)

def update_after_insert(cache_snapshot, obj):
    check_reset(cache_snapshot)
    
    global s_dist
    key = obj.key
    capacity = cache_snapshot.capacity
    
    # Adaptive sizing delta
    delta = 1.0 / capacity if capacity > 0 else 0.01
    
    if key in ghost_s:
        # Hit in Ghost S -> S was too small
        s_dist = min(0.9, s_dist + delta)
        # Rescue: promote to M
        m_queue[key] = None
        del ghost_s[key]
        accessed_bits.discard(key)
        
    elif key in ghost_m:
        # Hit in Ghost M -> M was too small (S too big)
        s_dist = max(0.01, s_dist - delta)
        # Rescue: promote to M
        m_queue[key] = None
        del ghost_m[key]
        accessed_bits.discard(key)
        
    else:
        # New insert -> S
        s_queue[key] = None
        accessed_bits.discard(key)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    # Cleanup consistency
    key = evicted_obj.key
    accessed_bits.discard(key)
    demoted_from_m.discard(key)
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