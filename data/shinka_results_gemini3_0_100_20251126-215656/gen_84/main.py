# EVOLVE-BLOCK-START
"""
S3-FIFO with Extended Ghost and Conservative Rescue.
Combines the extended history tracking of S3-FIFO-D-G with the clean,
no-bonus rescue logic of the Inspiration program to balance scan-resistance and loop-capture.
"""

from collections import OrderedDict

# Global structures
# s_queue: Small FIFO queue (probationary)
# m_queue: Main FIFO queue (protected)
# ghost_registry: Keeps track of recently evicted keys to detect loops/recurrence
# freq_map: Tracks access frequency (0-3) for items

s_queue = OrderedDict()
m_queue = OrderedDict()
ghost_registry = OrderedDict()
freq_map = {}

def reset_globals_if_new_trace(cache_snapshot):
    """
    Heuristic to reset global state if a new trace starts.
    We detect this if the cache size is very small (start of trace)
    but our internal queues have leftover data.
    """
    global s_queue, m_queue, ghost_registry, freq_map
    if len(cache_snapshot.cache) <= 1 and (len(s_queue) > 1 or len(m_queue) > 1):
        s_queue.clear()
        m_queue.clear()
        ghost_registry.clear()
        freq_map.clear()

def evict(cache_snapshot, obj):
    '''
    Executes the S3-FIFO eviction policy with frequency-based decisions and extended ghost.
    '''
    capacity = cache_snapshot.capacity
    # Target size for the small queue (10% of capacity, min 1)
    s_target = max(int(capacity * 0.1), 1)

    # Safety check for state consistency
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
            freq = freq_map.get(candidate_key, 0)

            if freq > 0:
                # Hit in S -> Promote to M
                m_queue[candidate_key] = None
                freq_map[candidate_key] = 0 # Reset freq on promotion
            else:
                # Victim found in S
                ghost_registry[candidate_key] = None
                # Extended Ghost: 5x Capacity for better loop detection
                if len(ghost_registry) > capacity * 5:
                    ghost_registry.popitem(last=False)
                return candidate_key

        # 2. Evict from Main Queue
        else:
            candidate_key, _ = m_queue.popitem(last=False) # FIFO head
            freq = freq_map.get(candidate_key, 0)

            if freq > 0:
                # Hit in M -> Reinsert at tail (Decay frequency)
                m_queue[candidate_key] = None
                freq_map[candidate_key] = freq - 1
            else:
                # Victim found in M -> Conditional Demotion
                if len(s_queue) < s_target:
                    s_queue[candidate_key] = None
                else:
                    ghost_registry[candidate_key] = None
                    if len(ghost_registry) > capacity * 5:
                        ghost_registry.popitem(last=False)
                    return candidate_key

def update_after_hit(cache_snapshot, obj):
    '''
    Increment access frequency, capped at 3.
    '''
    key = obj.key
    freq_map[key] = min(freq_map.get(key, 0) + 1, 3)

def update_after_insert(cache_snapshot, obj):
    '''
    Handle insertion:
    - Reset globals if needed.
    - Check Ghost for rescue (M) with tenancy bonus.
    - Else insert to Probation (S).
    '''
    reset_globals_if_new_trace(cache_snapshot)

    key = obj.key

    if key in ghost_registry:
        # Rescue from Ghost -> Insert directly to Main
        m_queue[key] = None
        del ghost_registry[key]
        # Tenancy Bonus: Give rescued items a buffer (freq=1)
        # This allows them to survive one round in M (reinsertion) before needing a hit.
        freq_map[key] = 1
    else:
        # New item -> Insert into Small (Probation)
        s_queue[key] = None
        freq_map[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Clean up internal state after eviction.
    '''
    key = evicted_obj.key
    freq_map.pop(key, None)
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