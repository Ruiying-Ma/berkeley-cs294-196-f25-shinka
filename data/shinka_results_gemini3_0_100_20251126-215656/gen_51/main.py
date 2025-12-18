# EVOLVE-BLOCK-START
"""
Optimized S3-FIFO (Simple, Scalable, Static) with Conditional Demotion.
Implements a segmented cache with a small probationary queue (S) and a main protected queue (M).
Key Features:
- Conditional Demotion: M-victims are only demoted to S if S is not full, preserving S for new insertions.
- Ghost Registry: Tracks evicted items to rescue returning loop patterns directly to M.
- One-Hit Promotion: Simplifies promotion logic to capture working sets faster.
- Auto-Reset: Detects new trace starts to clear global state.
"""

from collections import OrderedDict

# Global structures
# s_queue: Small FIFO queue (probationary)
# m_queue: Main FIFO queue (protected)
# ghost_registry: Keeps track of recently evicted keys (True=from M, False=from S)
# frequencies: Tracks access frequency counts for items in cache

from collections import defaultdict

s_queue = OrderedDict()
m_queue = OrderedDict()
ghost_registry = OrderedDict()
frequencies = defaultdict(int)

def reset_globals_if_new_trace(cache_snapshot):
    """
    Heuristic to reset global state if a new trace starts.
    """
    global s_queue, m_queue, ghost_registry, frequencies
    if len(cache_snapshot.cache) <= 1 and (len(s_queue) > 1 or len(m_queue) > 1):
        s_queue.clear()
        m_queue.clear()
        ghost_registry.clear()
        frequencies.clear()

def evict(cache_snapshot, obj):
    '''
    Executes S3-FIFO with 2-bit frequency tracking, extended ghosts, and adaptive promotion.
    '''
    capacity = cache_snapshot.capacity
    s_target = max(int(capacity * 0.1), 1)
    ghost_capacity = capacity * 4  # Expanded ghost capacity for loop capture

    # Safety check
    if not s_queue and not m_queue:
        return next(iter(cache_snapshot.cache))

    while True:
        # 1. Evict from Small Queue
        if len(s_queue) > s_target or len(m_queue) == 0:
            if not s_queue:
                if m_queue:
                    candidate_key, _ = m_queue.popitem(last=False)
                    return candidate_key
                return next(iter(cache_snapshot.cache))

            candidate_key, _ = s_queue.popitem(last=False)
            freq = frequencies[candidate_key]

            # Adaptive Promotion:
            # If M is underutilized (< 50%), promote on 1 hit.
            # Otherwise, require 2 hits (freq >= 2) to filter one-hit wonders.
            promote_threshold = 1 if len(m_queue) < capacity * 0.5 else 2

            if freq >= promote_threshold:
                # Promote to M
                m_queue[candidate_key] = None
                frequencies[candidate_key] = 0 # Reset freq in M
            elif freq == 1 and promote_threshold == 2:
                # Second Chance in S (Extended Probation)
                s_queue[candidate_key] = None
                frequencies[candidate_key] = 0 # Consume hit, reset to 0
            else:
                # Victim found in S
                ghost_registry[candidate_key] = False # From S
                if len(ghost_registry) > ghost_capacity:
                    ghost_registry.popitem(last=False)
                return candidate_key

        # 2. Evict from Main Queue
        else:
            candidate_key, _ = m_queue.popitem(last=False)
            freq = frequencies[candidate_key]

            if freq > 0:
                # Second Chance in M
                m_queue[candidate_key] = None
                frequencies[candidate_key] = 0 # Reset
            else:
                # Victim found in M
                # Conditional Demotion: Demote to S only if S has room.
                if len(s_queue) < s_target:
                    s_queue[candidate_key] = None
                    frequencies[candidate_key] = 0
                else:
                    ghost_registry[candidate_key] = True # From M
                    if len(ghost_registry) > ghost_capacity:
                        ghost_registry.popitem(last=False)
                    return candidate_key

def update_after_hit(cache_snapshot, obj):
    '''
    Increment frequency up to cap.
    '''
    frequencies[obj.key] = min(frequencies[obj.key] + 1, 3)

def update_after_insert(cache_snapshot, obj):
    '''
    Handle insertion using Ghost signal for promotion.
    '''
    reset_globals_if_new_trace(cache_snapshot)

    key = obj.key

    if key in ghost_registry:
        # Ghost Hit: Strong signal of recurrence -> M
        # We ignore source (S vs M) and always promote to M to capture loops
        ghost_registry.pop(key)
        m_queue[key] = None
    else:
        # New item -> S
        s_queue[key] = None

    # Initialize frequency
    frequencies[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Cleanup.
    '''
    key = evicted_obj.key
    if key in frequencies:
        del frequencies[key]
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