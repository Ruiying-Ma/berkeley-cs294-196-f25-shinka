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
# ghost_registry: Ghost FIFO queue (history of evicted probationary items)
# accessed_bits: Set of keys accessed while in cache (simulating reference bits)
# probation_set: Set of keys in S that have been accessed once (hysteresis)

s_queue = OrderedDict()
m_queue = OrderedDict()
ghost_registry = OrderedDict()
accessed_bits = set()
probation_set = set()

def evict(cache_snapshot, obj):
    '''
    Selects a victim using S3-FIFO policy with hysteresis and improved ghost management.
    '''
    # Target size for the small queue (10% of capacity)
    s_capacity = max(int(cache_snapshot.capacity * 0.1), 1)

    while True:
        # Check S queue first if it exceeds capacity or if M is empty
        if len(s_queue) > s_capacity or (len(s_queue) > 0 and len(m_queue) == 0):
            candidate_key, _ = s_queue.popitem(last=False) # Pop from head (FIFO)

            if candidate_key in accessed_bits:
                # Hit in S
                accessed_bits.discard(candidate_key)
                if candidate_key in probation_set:
                    # Second hit (or subsequent): Promote to M
                    m_queue[candidate_key] = None
                    probation_set.discard(candidate_key)
                else:
                    # First hit: Reinsert to S for another chance (extend probation)
                    s_queue[candidate_key] = None
                    probation_set.add(candidate_key)
            else:
                # Victim found in S
                probation_set.discard(candidate_key)
                # Add to ghost registry
                ghost_registry[candidate_key] = None
                # Cap ghost size
                if len(ghost_registry) > cache_snapshot.capacity * 2:
                    ghost_registry.popitem(last=False)
                return candidate_key

        else:
            # Check M queue
            if not m_queue:
                # Fallback to S
                if s_queue:
                        k, _ = s_queue.popitem(last=False)
                        return k
                return next(iter(cache_snapshot.cache))

            candidate_key, _ = m_queue.popitem(last=False) # Pop from head

            if candidate_key in accessed_bits:
                # Second chance: Re-insert at tail of M
                accessed_bits.discard(candidate_key)
                m_queue[candidate_key] = None
            else:
                # Victim found in M
                # Remove demotion to S (prevent pollution)
                # Add to Ghost (helps with large working sets)
                ghost_registry[candidate_key] = None
                if len(ghost_registry) > cache_snapshot.capacity * 2:
                    ghost_registry.popitem(last=False)
                return candidate_key

def update_after_hit(cache_snapshot, obj):
    '''
    Mark the object as accessed.
    '''
    accessed_bits.add(obj.key)

def update_after_insert(cache_snapshot, obj):
    '''
    Insert new object into S or M based on ghost history.
    '''
    if obj.key in ghost_registry:
        # Promote directly to Main queue
        m_queue[obj.key] = None
        del ghost_registry[obj.key]
    else:
        # Insert into Small queue
        s_queue[obj.key] = None

    # Reset access bit on insert
    accessed_bits.discard(obj.key)
    # Ensure not in probation set
    probation_set.discard(obj.key)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Cleanup.
    '''
    accessed_bits.discard(evicted_obj.key)
    probation_set.discard(evicted_obj.key)
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