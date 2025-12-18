# EVOLVE-BLOCK-START
"""
S3-LRU Hybrid Cache Eviction Algorithm
Combines S3-FIFO's scan resistance with LRU's recency management for the main queue.
- Small Queue (S): FIFO with lazy promotion (Second Chance). Filters scans.
- Main Queue (M): Strict LRU. Manages popular items efficiently.
- Ghost Queue (G): Tracks history of evicted S items for quick promotion.
"""

from collections import OrderedDict

# Global State
# q_S: Small FIFO queue. Keys -> None
# q_M: Main LRU queue. Keys -> None
# g_S: Ghost queue for evictions from S. Keys -> None
# g_M: Ghost queue for evictions from M. Keys -> None
# accessed: Set of keys in S that have been accessed
# s_ratio: Ratio of cache dedicated to S
# last_access_count: Timestamp to detect new traces
q_S = OrderedDict()
q_M = OrderedDict()
g_S = OrderedDict()
g_M = OrderedDict()
accessed = set()
s_ratio = 0.1
last_access_count = -1

def _reset_state_if_needed(snapshot):
    """Resets global state if a new trace is detected based on access count dropping."""
    global q_S, q_M, g_S, g_M, accessed, s_ratio, last_access_count
    if snapshot.access_count < last_access_count:
        q_S.clear()
        q_M.clear()
        g_S.clear()
        g_M.clear()
        accessed.clear()
        s_ratio = 0.1
    last_access_count = snapshot.access_count

def evict(cache_snapshot, obj):
    '''
    Adaptive S3-LRU Eviction.
    - Adapts s_ratio based on ghost hits.
    - Evicts from S if S > target or M is empty.
    - Evicts from M otherwise.
    - Populates g_S and g_M on eviction.
    '''
    global q_S, q_M, g_S, g_M, accessed, s_ratio
    _reset_state_if_needed(cache_snapshot)

    capacity = cache_snapshot.capacity
    target_s = max(1, int(capacity * s_ratio))

    while True:
        # Check if we should evict from S
        evict_from_s = False
        if len(q_S) > target_s or not q_M:
            evict_from_s = True

        # Safety: if S is empty, forced to evict M
        if evict_from_s and not q_S:
            evict_from_s = False

        if evict_from_s:
            # S eviction
            candidate, _ = q_S.popitem(last=False)

            # Sync check
            if candidate not in cache_snapshot.cache:
                accessed.discard(candidate)
                continue

            if candidate in accessed:
                # Lazy promotion
                accessed.discard(candidate)
                q_M[candidate] = None # Move to MRU of M
                continue
            else:
                # Actual eviction from S -> g_S
                g_S[candidate] = None
                # Cap ghosts (approx capacity)
                if len(g_S) > capacity:
                    g_S.popitem(last=False)
                return candidate
        else:
            # M eviction
            if not q_M:
                # Should not happen if cache full and q_S checked
                if q_S: continue # Go back to evict S
                return None

            candidate, _ = q_M.popitem(last=False)

            if candidate not in cache_snapshot.cache:
                continue

            # Evict from M -> g_M
            g_M[candidate] = None
            if len(g_M) > capacity:
                g_M.popitem(last=False)
            return candidate

def update_after_hit(cache_snapshot, obj):
    '''
    On Hit:
    - M: Update LRU.
    - S: Mark accessed.
    '''
    global q_S, q_M, accessed
    _reset_state_if_needed(cache_snapshot)

    key = obj.key
    if key in q_M:
        q_M.move_to_end(key)
    elif key in q_S:
        accessed.add(key)

def update_after_insert(cache_snapshot, obj):
    '''
    On Insert:
    - Check Ghosts for adaptation.
    - Insert to S or M.
    '''
    global q_S, q_M, g_S, g_M, accessed, s_ratio
    _reset_state_if_needed(cache_snapshot)

    key = obj.key

    if key in g_S:
        # Hit in Ghost S -> S was too small
        s_ratio = min(0.9, s_ratio + 0.02)
        q_M[key] = None
        del g_S[key]
    elif key in g_M:
        # Hit in Ghost M -> M was too small (S too big)
        s_ratio = max(0.01, s_ratio - 0.02)
        q_M[key] = None
        del g_M[key]
    else:
        # New insert -> S
        q_S[key] = None
        accessed.discard(key)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Cleanup.
    '''
    global q_S, q_M, accessed
    _reset_state_if_needed(cache_snapshot)

    key = evicted_obj.key
    q_S.pop(key, None)
    q_M.pop(key, None)
    accessed.discard(key)
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