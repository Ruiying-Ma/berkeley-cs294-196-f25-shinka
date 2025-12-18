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
# access_counts: Dictionary mapping keys to frequency counters (0-3)

s_queue = OrderedDict()
m_queue = OrderedDict()
ghost_registry = OrderedDict()
access_counts = {}

def evict(cache_snapshot, obj):
    '''
    S3-FIFO eviction with Demotion and Frequency Decay.
    - S items with hits -> M (Frequency Reset)
    - M items with hits -> M (Frequency Decay)
    - M items without hits -> Demote to S (Second chance)
    - S items without hits -> Evict (Ghost)
    '''
    s_capacity = max(int(cache_snapshot.capacity * 0.1), 1)

    while True:
        # 1. Check Small Queue (Probation)
        # Prioritize clearing S if it's too big, or if M is empty
        if len(s_queue) > s_capacity or not m_queue:
            if not s_queue:
                # If S is empty but we are here, M must be empty too (or we'd be in else)
                if m_queue:
                    # Should not happen given condition, but safety fallback to M
                    pass
                else:
                    break # Both empty
            else:
                candidate_key, _ = s_queue.popitem(last=False)

                freq = access_counts.get(candidate_key, 0)
                if freq > 0:
                    # Promote to Main
                    m_queue[candidate_key] = None
                    access_counts[candidate_key] = 0 # Reset on promotion
                else:
                    # Evict from S -> Ghost
                    ghost_registry[candidate_key] = None
                    # Ghost management (2x capacity for better loop detection)
                    if len(ghost_registry) > cache_snapshot.capacity * 2:
                        ghost_registry.popitem(last=False)

                    if candidate_key in access_counts:
                        del access_counts[candidate_key]
                    return candidate_key
                continue

        # 2. Check Main Queue (Protected)
        # S is within capacity, check M
        candidate_key, _ = m_queue.popitem(last=False)

        freq = access_counts.get(candidate_key, 0)
        if freq > 0:
            # Re-insert in Main with decay
            m_queue[candidate_key] = None
            access_counts[candidate_key] = freq - 1
        else:
            # Demote to Small (Probation)
            # This allows M items to be compared against new S items before dying
            s_queue[candidate_key] = None
            access_counts[candidate_key] = 0
            # Loop continues, and since S grew, we might check S next

    # Fallback
    if cache_snapshot.cache:
        return next(iter(cache_snapshot.cache))
    return None

def update_after_hit(cache_snapshot, obj):
    '''
    Increment frequency on hit, capped at 3.
    '''
    curr = access_counts.get(obj.key, 0)
    access_counts[obj.key] = min(curr + 1, 3)

def update_after_insert(cache_snapshot, obj):
    '''
    Insert new object. Ghost hits go to Main, others to Small.
    '''
    if obj.key in ghost_registry:
        # Ghost Hit: Promote to Main
        m_queue[obj.key] = None
        access_counts[obj.key] = 0
        del ghost_registry[obj.key]
    else:
        # New Insert: Start in Small
        s_queue[obj.key] = None
        access_counts[obj.key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Cleanup metadata.
    '''
    k = evicted_obj.key
    if k in access_counts:
        del access_counts[k]
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