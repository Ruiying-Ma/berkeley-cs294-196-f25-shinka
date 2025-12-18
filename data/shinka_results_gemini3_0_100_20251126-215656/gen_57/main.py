# EVOLVE-BLOCK-START
"""
Tiered Ghost S3-FIFO (T-S3-FIFO)
Advanced segmented cache with origin-tracking ghost registry and tiered rescue policies.
- S-Queue: Probationary FIFO.
- M-Queue: Protected FIFO.
- Ghost: Tracks eviction history (3x capacity) with origin bit (S vs M).
- Tiered Rescue: M-ghosts promote to M. S-ghosts retry in S with a 'credit'.
"""

from collections import OrderedDict

# Global structures
s_queue = OrderedDict()          # Small/Probationary FIFO
m_queue = OrderedDict()          # Main/Protected FIFO
ghost_registry = OrderedDict()   # Key -> Bool (True=from M, False=from S)
accessed_bits = set()            # Tracks access status

def reset_globals_if_new_trace(cache_snapshot):
    """Reset global state when a new trace is detected."""
    global s_queue, m_queue, ghost_registry, accessed_bits
    if len(cache_snapshot.cache) <= 1 and (len(s_queue) > 1 or len(m_queue) > 1):
        s_queue.clear()
        m_queue.clear()
        ghost_registry.clear()
        accessed_bits.clear()

def evict(cache_snapshot, obj):
    '''
    Eviction logic with S/M queues and Ghost management.
    '''
    capacity = cache_snapshot.capacity
    # Target size for S: 10% of capacity
    s_target = max(int(capacity * 0.1), 1)
    
    # Consistency check
    if not s_queue and not m_queue:
        return next(iter(cache_snapshot.cache))
    
    while True:
        # Determine eviction source
        # Evict from S if it's over target size OR if M is empty
        evict_from_s = (len(s_queue) > s_target) or (len(m_queue) == 0)
        
        if evict_from_s:
            if not s_queue:
                # Fallback: if S is empty but we decided to evict from S, 
                # it means M is also empty (checked above).
                if m_queue:
                    candidate, _ = m_queue.popitem(last=False)
                    return candidate
                return next(iter(cache_snapshot.cache))

            candidate, _ = s_queue.popitem(last=False) # FIFO head
            
            if candidate in accessed_bits:
                # Hit in S (or Ghost rescue credit) -> Promote to M
                accessed_bits.discard(candidate)
                m_queue[candidate] = None
                continue # Re-evaluate
            else:
                # Evict from S -> Ghost (Origin=False)
                ghost_registry[candidate] = False
                # Manage Ghost Size (3x capacity)
                if len(ghost_registry) > capacity * 3:
                    ghost_registry.popitem(last=False)
                return candidate
        
        else:
            # Evict from M
            candidate, _ = m_queue.popitem(last=False)
            
            if candidate in accessed_bits:
                # Hit in M -> Reinsert at tail (Second Chance)
                accessed_bits.discard(candidate)
                m_queue[candidate] = None
                continue
            else:
                # Victim found in M
                # Conditional Demotion: Only if S is below target
                # This prevents flushing M items into a full S (scan resistance)
                if len(s_queue) < s_target:
                    s_queue[candidate] = None
                    continue # Demoted, loop again to find victim
                else:
                    # Drop and add to Ghost (Origin=True)
                    ghost_registry[candidate] = True
                    if len(ghost_registry) > capacity * 3:
                        ghost_registry.popitem(last=False)
                    return candidate

def update_after_hit(cache_snapshot, obj):
    accessed_bits.add(obj.key)

def update_after_insert(cache_snapshot, obj):
    reset_globals_if_new_trace(cache_snapshot)
    
    key = obj.key
    
    # Check Ghost for rescue
    if key in ghost_registry:
        origin_m = ghost_registry.pop(key)
        if origin_m:
            # Origin M: Restore directly to M
            # Implicitly gives it a chance in M
            m_queue[key] = None
            accessed_bits.discard(key) 
        else:
            # Origin S: Restore to S, but give credit (accessed=True)
            # This ensures it promotes to M if it survives S
            s_queue[key] = None
            accessed_bits.add(key)
    else:
        # New item -> Insert into S (Probation)
        s_queue[key] = None
        accessed_bits.discard(key)

def update_after_evict(cache_snapshot, obj, evicted_obj):
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