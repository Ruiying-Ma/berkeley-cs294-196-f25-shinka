# EVOLVE-BLOCK-START
"""
S3-FIFO Eviction Algorithm
Uses a Small (S) queue for new items and a Main (M) queue for frequent items.
Items in S are evicted early (scan resistance) unless accessed or previously in Ghost (G).
Items in M are given second chances upon access.
"""

class S3FIFOState:
    def __init__(self, cache_id):
        self.cache_id = cache_id
        self.small = dict()   # Small FIFO queue (approx 10%)
        self.main = dict()    # Main FIFO queue (approx 90%)
        self.ghost = dict()   # Ghost FIFO queue
        self.accessed = dict() # Track access bits
        self.capacity = 0

_state = None

def get_state(cache_snapshot):
    global _state
    current_id = id(cache_snapshot.cache)
    if _state is None or _state.cache_id != current_id:
        _state = S3FIFOState(current_id)

    # Sync check
    state_count = len(_state.small) + len(_state.main)
    cache_count = len(cache_snapshot.cache)
    if abs(state_count - cache_count) > 5:
        _state = S3FIFOState(current_id)
        # Heuristic recovery: put all in Main
        for k in cache_snapshot.cache:
            _state.main[k] = None
            _state.accessed[k] = False

    _state.capacity = cache_snapshot.capacity
    return _state

def evict(cache_snapshot, obj):
    '''
    S3-FIFO Eviction Logic
    '''
    state = get_state(cache_snapshot)
    target_small = max(1, int(state.capacity * 0.1))

    # Loop to find victim, handling lazy promotions/reinsertions
    loops = 0
    max_loops = len(cache_snapshot.cache) * 3 + 20 # Safety limit

    while loops < max_loops:
        loops += 1

        # Determine which queue to operate on
        if len(state.small) > target_small or not state.main:
            # Check Small queue
            if not state.small:
                # Fallback
                if state.main: return next(iter(state.main))
                return None

            candidate = next(iter(state.small))
            if state.accessed.get(candidate, False):
                # Second chance: promote to Main
                del state.small[candidate]
                state.main[candidate] = None
                state.accessed[candidate] = False
            else:
                return candidate
        else:
            # Check Main queue
            if not state.main:
                if state.small: return next(iter(state.small))
                return None

            candidate = next(iter(state.main))
            if state.accessed.get(candidate, False):
                # Second chance: reinsert to Main
                del state.main[candidate]
                state.main[candidate] = None
                state.accessed[candidate] = False
            else:
                return candidate

    # Emergency fallback
    if cache_snapshot.cache:
        return next(iter(cache_snapshot.cache))
    return None

def update_after_hit(cache_snapshot, obj):
    '''
    Hit: Set accessed bit. Lazy promotion happens at eviction time.
    '''
    state = get_state(cache_snapshot)
    state.accessed[obj.key] = True

def update_after_insert(cache_snapshot, obj):
    '''
    Insert: Add to Small, or Main if in Ghost.
    '''
    state = get_state(cache_snapshot)
    key = obj.key
    state.accessed[key] = False

    if key in state.ghost:
        state.main[key] = None
        del state.ghost[key]
    else:
        state.small[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Evict: Clean up queues and update Ghost if evicted from Small.
    '''
    state = get_state(cache_snapshot)
    key = evicted_obj.key

    if key in state.small:
        del state.small[key]
        state.ghost[key] = None # Evicted from S -> G
    elif key in state.main:
        del state.main[key]
        # Evicted from M -> Gone

    if key in state.accessed:
        del state.accessed[key]

    # Bound Ghost size
    while len(state.ghost) > state.capacity:
        del state.ghost[next(iter(state.ghost))]
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