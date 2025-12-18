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
# Using OrderedDict as ordered sets
# q_S: Small FIFO queue. Keys -> None
# q_M: Main LRU queue. Keys -> None
# q_G: Ghost FIFO queue. Keys -> None
# accessed: Set of keys in S that have been accessed
# last_access_count: Timestamp to detect new traces
q_S = OrderedDict()
q_M = OrderedDict()
q_G = OrderedDict()
accessed = set()
last_access_count = -1

def _reset_state_if_needed(snapshot):
    """Resets global state if a new trace is detected based on access count dropping."""
    global q_S, q_M, q_G, accessed, last_access_count
    if snapshot.access_count < last_access_count:
        q_S.clear()
        q_M.clear()
        q_G.clear()
        accessed.clear()
    last_access_count = snapshot.access_count

def evict(cache_snapshot, obj):
    '''
    Selects a victim.
    - If S is larger than target (10%) or M is empty:
      - Evict from S (FIFO).
      - If S-head was accessed, promote to M (tail) and retry.
      - Else, evict S-head and add to Ghost.
    - Else:
      - Evict from M (LRU).
    '''
    global q_S, q_M, q_G, accessed
    _reset_state_if_needed(cache_snapshot)

    # Target size for S
    # We use current cache size as proxy for capacity when filling up,
    # but strictly it should be based on capacity.
    # Using len(cache) allows dynamic growth during warmup.
    target_s = max(1, int(len(cache_snapshot.cache) * 0.1))

    while True:
        # Logic: Prefer evicting from S if it's over budget
        # Or if M is empty (must evict from S)
        if len(q_S) > target_s or not q_M:
            if not q_S:
                # Fallback if both empty (should not happen in full cache)
                if q_M:
                    return q_M.popitem(last=False)[0]
                return next(iter(cache_snapshot.cache))

            candidate, _ = q_S.popitem(last=False) # FIFO head

            # Sync check: ensure candidate is actually in cache
            if candidate not in cache_snapshot.cache:
                accessed.discard(candidate)
                continue

            if candidate in accessed:
                # Lazy promotion to Main
                accessed.discard(candidate)
                q_M[candidate] = None # Add to MRU of Main
                continue
            else:
                # Evict candidate
                # Add to Ghost
                q_G[candidate] = None
                if len(q_G) > cache_snapshot.capacity:
                    q_G.popitem(last=False) # Cap Ghost size
                return candidate

        else:
            # Evict from Main
            # M is LRU, so victim is at head
            candidate, _ = q_M.popitem(last=False)

            if candidate not in cache_snapshot.cache:
                continue

            return candidate

def update_after_hit(cache_snapshot, obj):
    '''
    On Hit:
    - If in M: Move to MRU (LRU policy).
    - If in S: Mark accessed (Lazy promotion).
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
    - If in Ghost: Insert to M (MRU).
    - Else: Insert to S (MRU).
    '''
    global q_S, q_M, q_G, accessed
    _reset_state_if_needed(cache_snapshot)

    key = obj.key
    if key in q_G:
        q_M[key] = None
        del q_G[key]
    else:
        q_S[key] = None
        accessed.discard(key)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Cleanup internal state for evicted object.
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