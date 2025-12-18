# EVOLVE-BLOCK-START
"""
S3-FIFO Cache Eviction Algorithm Implementation.
Inspired by S3-FIFO (Simple, Scalable, Static) which uses a small FIFO queue (S)
and a main FIFO queue (M) with re-insertion to approximate LRU with scan resistance
and frequency awareness.
"""

from collections import OrderedDict

# Global metadata structures
# s_queue: Small FIFO queue (probationary)
# m_queue: Main FIFO queue (protected)
# ghost_registry: Ghost FIFO queue (history of evicted items)
# freq_map: Map of object keys to access frequency (0-3)

s_queue = OrderedDict()
m_queue = OrderedDict()
ghost_registry = OrderedDict()
freq_map = {}

def evict(cache_snapshot, obj):
    '''
    Selects a victim using an enhanced S3-FIFO policy with frequency tracking
    and conditional demotion.
    '''
    # Target size for the small queue (10% of capacity)
    capacity = cache_snapshot.capacity
    s_capacity = max(int(capacity * 0.1), 1)

    # Enhanced Ghost Capacity (10x) to capture larger cycles
    ghost_capacity = capacity * 10

    while True:
        # Check S queue first if it exceeds capacity or if M is empty
        if len(s_queue) > s_capacity or (len(s_queue) > 0 and len(m_queue) == 0):
            candidate_key, _ = s_queue.popitem(last=False) # Pop from head
            freq = freq_map.get(candidate_key, 0)

            if freq > 0:
                # Hit in S: Promote to M
                m_queue[candidate_key] = None
                freq_map[candidate_key] = 0 # Reset frequency
            else:
                # Victim found in S
                # Add to ghost registry
                ghost_registry[candidate_key] = None
                if len(ghost_registry) > ghost_capacity:
                    ghost_registry.popitem(last=False)

                # Cleanup freq map
                if candidate_key in freq_map:
                    del freq_map[candidate_key]
                return candidate_key

        else:
            # Check M queue
            if not m_queue:
                # Fallback to S if M is empty
                if s_queue:
                    k, _ = s_queue.popitem(last=False)
                    if k in freq_map: del freq_map[k]
                    return k
                return next(iter(cache_snapshot.cache))

            candidate_key, _ = m_queue.popitem(last=False) # Pop from head
            freq = freq_map.get(candidate_key, 0)

            if freq > 0:
                # Hit in M: Re-insert at tail of M (Second Chance)
                m_queue[candidate_key] = None
                freq_map[candidate_key] = 0 # Reset frequency
            else:
                # Cold in M
                # Conditional Demotion: Demote to S only if S has space
                # This protects S from being flooded by cold M items
                if len(s_queue) < s_capacity:
                    s_queue[candidate_key] = None
                    # Keep freq as 0
                else:
                    # S is full, evict M item directly
                    # Also add to Ghost to track large working sets that spill over M
                    ghost_registry[candidate_key] = None
                    if len(ghost_registry) > ghost_capacity:
                        ghost_registry.popitem(last=False)

                    if candidate_key in freq_map:
                        del freq_map[candidate_key]
                    return candidate_key

def update_after_hit(cache_snapshot, obj):
    '''
    Increment access frequency, capped at 3.
    '''
    freq_map[obj.key] = min(freq_map.get(obj.key, 0) + 1, 3)

def update_after_insert(cache_snapshot, obj):
    '''
    Insert new object into S or M based on ghost history.
    '''
    key = obj.key
    freq_map[key] = 0 # Initialize frequency

    if key in ghost_registry:
        # Ghost hit -> Promote directly to Main queue
        m_queue[key] = None
        del ghost_registry[key]
    else:
        # New -> Insert into Small queue
        s_queue[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Cleanup.
    '''
    if evicted_obj.key in freq_map:
        del freq_map[evicted_obj.key]
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