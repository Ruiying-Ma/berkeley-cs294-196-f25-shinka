# EVOLVE-BLOCK-START
"""
Adaptive S3-FIFO (A-S3-FIFO)
Combines S3-FIFO with Adaptive Replacement Cache (ARC) principles.
Dynamically adjusts the size of the small/probationary queue (S) based on hits in ghost registries.
Uses two ghost queues:
- Ghost S: Tracks items evicted from S. Hits here imply S is too small.
- Ghost M: Tracks items evicted from M. Hits here imply M is too small (S is too big).
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
    Heuristic to reset globals if a new trace has started.
    Checks if cache is nearly empty but internal state is large.
    """
    global s_queue, m_queue, ghost_s, ghost_m, accessed_bits, s_dist
    # If cache has <= 1 items (start of trace) but we have history, reset.
    if len(cache_snapshot.cache) <= 1 and (len(s_queue) > 1 or len(m_queue) > 1):
        s_queue.clear()
        m_queue.clear()
        ghost_s.clear()
        ghost_m.clear()
        accessed_bits.clear()
        s_dist = 0.1

def evict(cache_snapshot, obj):
    '''
    Selects a victim using Adaptive S3-FIFO logic.
    Adjusts eviction source based on s_dist target.
    '''
    global s_dist

    capacity = cache_snapshot.capacity
    # Target size for S based on adaptive distribution
    s_target = max(1, int(capacity * s_dist))

    while True:
        # Determine if we should evict from S or M
        # Evict from S if it exceeds its target share, or if M is empty
        evict_s = (len(s_queue) > s_target) or (len(m_queue) == 0)

        if evict_s:
            if not s_queue:
                 # Should not happen if cache is full
                 return next(iter(cache_snapshot.cache))

            key, _ = s_queue.popitem(last=False) # FIFO head

            if key in accessed_bits:
                # Second chance: promote to M
                accessed_bits.discard(key)
                m_queue[key] = None
            else:
                # Evict from S -> Ghost S
                ghost_s[key] = None
                # Cap ghost size
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
                # Evict from M -> Ghost M
                ghost_m[key] = None
                # Cap ghost size
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

    # Calculate adaptation step size (1 slot equivalent)
    delta = 1.0 / capacity if capacity > 0 else 0.01

    if key in ghost_s:
        # Was in S, evicted, now back. S was too small.
        # Increase S target
        s_dist = min(0.9, s_dist + delta)
        # Promote to M (rescue)
        m_queue[key] = None
        del ghost_s[key]

    elif key in ghost_m:
        # Was in M, evicted, now back. M was too small.
        # Decrease S target (grow M)
        s_dist = max(0.01, s_dist - delta)
        # Promote to M (rescue)
        m_queue[key] = None
        del ghost_m[key]

    else:
        # New item -> S
        s_queue[key] = None

    # Reset access bit for new/promoted item
    accessed_bits.discard(key)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    accessed_bits.discard(evicted_obj.key)
    # Ensure consistency (cleanup if needed)
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