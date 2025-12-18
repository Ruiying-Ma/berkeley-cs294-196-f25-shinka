# EVOLVE-BLOCK-START
"""
S3-FIFO Cache Eviction Algorithm
Uses a simplified S3-FIFO approach with two queues (S and M) and lazy promotion.
- S (Small): Captures new insertions. Filters out scans and 'one-hit wonders'.
- M (Main): Holds frequently accessed items.
- Access bits: tracked via 'accessed' set.
Promotions occur lazily during eviction: if an eviction candidate in S or M has been accessed,
it is moved to M (giving it a second chance) and the access bit is cleared.
"""
from collections import deque

class S3FIFOState:
    def __init__(self, cache_id):
        self.cache_id = cache_id
        self.S = deque()
        self.M = deque()
        self.accessed = set()
        self.victim_queue = None

    def reset(self):
        self.S.clear()
        self.M.clear()
        self.accessed.clear()
        self.victim_queue = None

_state = None

def get_state(cache_snapshot):
    global _state
    # Use id of cache dict to identify the cache instance (handles multiple traces)
    current_id = id(cache_snapshot.cache)

    if _state is None or _state.cache_id != current_id:
        _state = S3FIFOState(current_id)

    # Consistency check: If state has vastly more items than cache, it's likely stale data
    # from a previous run where memory was reused (same id).
    # We allow a small slack (e.g., 5) for transient states during eviction/insertion.
    cache_len = len(cache_snapshot.cache)
    state_len = len(_state.S) + len(_state.M)
    if state_len > cache_len + 5:
        _state = S3FIFOState(current_id)

    return _state

def evict(cache_snapshot, obj):
    '''
    Determines the eviction victim using S3-FIFO logic.
    '''
    state = get_state(cache_snapshot)

    # S queue target size: 10% of total cache count
    cache_count = len(cache_snapshot.cache)
    s_capacity = max(1, int(cache_count * 0.1))

    while True:
        # Check S if it's oversized or if M is empty
        # This prioritizes cleaning up S (scan resistance)
        check_s = len(state.S) >= s_capacity or len(state.M) == 0

        queue = state.S if check_s else state.M
        q_name = 'S' if check_s else 'M'

        if not queue:
            # Fallback for safety (e.g. if state desyncs or empty cache logic)
            if cache_snapshot.cache:
                return next(iter(cache_snapshot.cache))
            return None # Should not be reached

        candidate = queue[-1] # Inspect Tail

        # Robustness: verify candidate is actually in the cache
        if candidate not in cache_snapshot.cache:
            queue.pop() # Remove phantom entry
            state.accessed.discard(candidate)
            continue

        if candidate in state.accessed:
            # Second Chance: Reinsert into M (Main) head and clear access bit
            state.accessed.remove(candidate)
            queue.pop()
            state.M.appendleft(candidate)
            # Loop continues to search for a victim
        else:
            # Found a victim (not accessed since insertion/promotion)
            state.victim_queue = q_name
            return candidate

def update_after_hit(cache_snapshot, obj):
    '''
    Mark object as accessed. Promotion is lazy (happens at eviction).
    '''
    state = get_state(cache_snapshot)
    state.accessed.add(obj.key)

def update_after_insert(cache_snapshot, obj):
    '''
    Insert new object into S (Small) queue.
    '''
    state = get_state(cache_snapshot)
    state.S.appendleft(obj.key)
    state.accessed.discard(obj.key) # Initially not accessed

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Clean up the evicted object from the internal queues.
    '''
    state = get_state(cache_snapshot)

    # Remove from the queue we decided to evict from
    if state.victim_queue == 'S':
        # Optimistically check tail (O(1))
        if state.S and state.S[-1] == evicted_obj.key:
            state.S.pop()
        else:
            # Fallback removal (O(N))
            try: state.S.remove(evicted_obj.key)
            except ValueError: pass

    elif state.victim_queue == 'M':
        if state.M and state.M[-1] == evicted_obj.key:
            state.M.pop()
        else:
            try: state.M.remove(evicted_obj.key)
            except ValueError: pass

    # Cleanup metadata
    state.accessed.discard(evicted_obj.key)
    state.victim_queue = None
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