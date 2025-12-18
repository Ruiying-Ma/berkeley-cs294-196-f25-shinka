# EVOLVE-BLOCK-START
"""
Robust S3-FIFO (RobustS3FIFO)
Combines the high-performance "Always Demote" strategy of the current implementation
with the robustness of global state resetting from the Optimized variant.
Increases ghost registry size to improve long-range loop detection.
"""

from collections import OrderedDict

# Global metadata structures
s_queue = OrderedDict()       # Small/Probationary Queue
m_queue = OrderedDict()       # Main/Protected Queue
ghost_registry = OrderedDict() # Ghost Registry
accessed_bits = set()         # Accessed bits (simulating reference bits)

def reset_state_if_needed(cache_snapshot):
    """
    Detects start of a new trace to reset global state.
    Called on insert. If cache is virtually empty (<=1 item) but queues are populated,
    it implies a stale state from a previous run.
    """
    # If the simulator has cleared the cache (len <= 1) but we have history, reset.
    if len(cache_snapshot.cache) <= 1 and (len(s_queue) > 0 or len(m_queue) > 0):
        s_queue.clear()
        m_queue.clear()
        ghost_registry.clear()
        accessed_bits.clear()

def evict(cache_snapshot, obj):
    '''
    S3-FIFO Eviction Policy.
    - S-Queue: Probationary. 10% target size.
    - M-Queue: Protected.
    - Policy:
        1. If S > Target or M is empty: Evict from S.
           - If S-victim accessed: Promote to M.
           - Else: Evict and record in Ghost.
        2. Else (S <= Target and M has items): Evict from M.
           - If M-victim accessed: Reinsert to M (Second Chance).
           - Else: Demote to S (tail). Gives one full pass through S.
    '''
    # Target size for S queue (10% of total capacity)
    capacity = cache_snapshot.capacity
    s_target = max(int(capacity * 0.1), 1)
    
    # Ghost limit: Increased to 3x to catch longer loops
    ghost_limit = capacity * 3

    while True:
        # 1. Clean S if it's too big or if M is empty (balance requirement)
        if len(s_queue) > s_target or not m_queue:
            if s_queue:
                candidate, _ = s_queue.popitem(last=False) # LRU of S
                
                if candidate in accessed_bits:
                    # Promote to M
                    accessed_bits.discard(candidate)
                    m_queue[candidate] = None
                else:
                    # Evict from S
                    ghost_registry[candidate] = None
                    if len(ghost_registry) > ghost_limit:
                        ghost_registry.popitem(last=False)
                    return candidate
            else:
                # Fallback: S is empty, M is empty (shouldn't happen in full cache)
                return next(iter(cache_snapshot.cache))

        # 2. Clean M
        else:
            # M is not empty
            candidate, _ = m_queue.popitem(last=False) # LRU of M
            
            if candidate in accessed_bits:
                # Second Chance in M
                accessed_bits.discard(candidate)
                m_queue[candidate] = None
            else:
                # Demote to S
                # "Always Demote" logic: Give M-victims a chance in S.
                # This helps scan resistance by buffering them, 
                # and loop resistance by extending lifetime.
                s_queue[candidate] = None
                # We haven't evicted yet, so loop continues to check S again.

def update_after_hit(cache_snapshot, obj):
    '''Mark access bit.'''
    accessed_bits.add(obj.key)

def update_after_insert(cache_snapshot, obj):
    '''
    Handle insertion.
    - Reset state if new trace.
    - Check Ghost: if hit, insert to M (rescue).
    - Else: insert to S (probation).
    '''
    reset_state_if_needed(cache_snapshot)
    
    key = obj.key
    # Reset access bit on new insert (assumed cold initially)
    accessed_bits.discard(key)
    
    if key in ghost_registry:
        # Ghost Hit: Promote to M
        m_queue[key] = None
        del ghost_registry[key]
    else:
        # New: Insert to S
        s_queue[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''Cleanup.'''
    key = evicted_obj.key
    accessed_bits.discard(key)
    # Keys should be removed from queues in evict(), but safe to ensure?
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