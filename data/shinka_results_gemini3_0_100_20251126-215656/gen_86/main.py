# EVOLVE-BLOCK-START
"""
S3-FIFO with Multi-bit Frequency, Expanded Ghost, and Tenancy-Aware Promotion.
Improvements:
1. Replaces boolean 'accessed_bits' with 'access_counts' (0-3) to distinguish hotness.
2. Expands Ghost Registry to 3x capacity to capture longer loops (Trace 14/29).
3. Tenancy-Aware: Rescued items start in Main with freq=0 (one pass protection),
   while hits in Main accumulate frequency for stronger resistance.
"""

from collections import OrderedDict

# Global structures
# s_queue: Small FIFO queue (probationary)
# m_queue: Main FIFO queue (protected)
# ghost_registry: Keeps track of recently evicted keys to detect loops/recurrence
# access_counts: Tracks frequency of access (0 to 3)

s_queue = OrderedDict()
m_queue = OrderedDict()
ghost_registry = OrderedDict()
access_counts = {}

def reset_globals_if_new_trace(cache_snapshot):
    """
    Heuristic to reset global state if a new trace starts.
    """
    global s_queue, m_queue, ghost_registry, access_counts
    if len(cache_snapshot.cache) <= 1 and (len(s_queue) > 1 or len(m_queue) > 1):
        s_queue.clear()
        m_queue.clear()
        ghost_registry.clear()
        access_counts.clear()

def evict(cache_snapshot, obj):
    '''
    Executes the S3-FIFO eviction policy with multi-bit frequency and conditional demotion.
    '''
    capacity = cache_snapshot.capacity
    # Target size for the small queue (10% of capacity, min 1)
    s_target = max(int(capacity * 0.1), 1)

    # Safety check
    if not s_queue and not m_queue:
        return next(iter(cache_snapshot.cache))

    while True:
        # 1. Evict from Small Queue if it's too big OR Main is empty
        if len(s_queue) > s_target or len(m_queue) == 0:
            if not s_queue:
                if m_queue:
                    candidate_key, _ = m_queue.popitem(last=False)
                    return candidate_key
                return next(iter(cache_snapshot.cache))

            candidate_key, _ = s_queue.popitem(last=False) # FIFO head

            freq = access_counts.get(candidate_key, 0)
            if freq > 0:
                # Hit in S -> Promote to M
                # Reset frequency to 0 on promotion to M (must prove worth in M)
                access_counts[candidate_key] = 0
                m_queue[candidate_key] = None
            else:
                # Victim found in S
                ghost_registry[candidate_key] = None
                # Expanded Ghost Size: 3x Capacity
                if len(ghost_registry) > capacity * 3:
                    ghost_registry.popitem(last=False)
                # Cleanup counts
                if candidate_key in access_counts:
                    del access_counts[candidate_key]
                return candidate_key

        # 2. Evict from Main Queue
        else:
            candidate_key, _ = m_queue.popitem(last=False) # FIFO head

            freq = access_counts.get(candidate_key, 0)
            if freq > 0:
                # Hit in M -> Reinsert at tail (Second Chance)
                # Decay frequency
                access_counts[candidate_key] = freq - 1
                m_queue[candidate_key] = None
            else:
                # Victim found in M (freq=0)
                # Conditional Demotion
                if len(s_queue) < s_target:
                    s_queue[candidate_key] = None
                else:
                    ghost_registry[candidate_key] = None
                    if len(ghost_registry) > capacity * 3:
                        ghost_registry.popitem(last=False)
                    if candidate_key in access_counts:
                        del access_counts[candidate_key]
                    return candidate_key

def update_after_hit(cache_snapshot, obj):
    '''
    Increment frequency, capped at 3.
    '''
    key = obj.key
    curr = access_counts.get(key, 0)
    if curr < 3:
        access_counts[key] = curr + 1

def update_after_insert(cache_snapshot, obj):
    '''
    Handle insertion.
    '''
    reset_globals_if_new_trace(cache_snapshot)

    key = obj.key

    if key in ghost_registry:
        # Rescue from Ghost -> Insert directly to Main
        m_queue[key] = None
        del ghost_registry[key]
        # Start with 0 freq (one pass protection)
        access_counts[key] = 0
    else:
        # New item -> Insert into Small (Probation)
        s_queue[key] = None
        # Start cold
        access_counts[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Clean up internal state after eviction.
    '''
    key = evicted_obj.key
    if key in access_counts:
        del access_counts[key]
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