# EVOLVE-BLOCK-START
"""
ARC (Adaptive Replacement Cache) Eviction Algorithm
Maintains two LRU lists: T1 (recency) and T2 (frequency), and two ghost lists B1 and B2.
Dynamically adjusts the target size `p` for T1 based on hits in the ghost lists.
"""

class ARCState:
    def __init__(self, cache_id):
        self.cache_id = cache_id
        # Use dicts as ordered sets for O(1) access and LRU preservation
        self.T1 = dict() # Recency (L1 cache)
        self.T2 = dict() # Frequency (L2 cache)
        self.B1 = dict() # Ghost Recency
        self.B2 = dict() # Ghost Frequency
        self.p = 0.0     # Target size for T1

_state = None

def get_state(cache_snapshot):
    global _state
    current_id = id(cache_snapshot.cache)
    if _state is None or _state.cache_id != current_id:
        _state = ARCState(current_id)

    # Sync check: if state deviates significantly from cache
    state_count = len(_state.T1) + len(_state.T2)
    cache_count = len(cache_snapshot.cache)

    if abs(state_count - cache_count) > 5:
        # Re-initialize from cache content
        _state = ARCState(current_id)
        for k in cache_snapshot.cache:
            # Assume recency for unknown state
            _state.T1[k] = None
        _state.p = 0.0

    return _state

def evict(cache_snapshot, obj):
    '''
    ARC eviction logic: adjusts p based on ghosts, then selects T1 or T2 victim.
    '''
    state = get_state(cache_snapshot)
    c = cache_snapshot.capacity
    key = obj.key

    # 1. Adapt p if hit in ghosts
    if key in state.B1:
        delta = 1
        if len(state.B1) >= len(state.B2) and len(state.B2) > 0:
             delta = 1
        elif len(state.B2) > len(state.B1):
             delta = len(state.B2) / len(state.B1)
        state.p = min(c, state.p + delta)
        # Move to MRU in B1 to protect from eviction in update_after_evict
        del state.B1[key]
        state.B1[key] = None

    elif key in state.B2:
        delta = 1
        if len(state.B2) >= len(state.B1) and len(state.B1) > 0:
             delta = 1
        elif len(state.B1) > len(state.B2):
             delta = len(state.B1) / len(state.B2)
        state.p = max(0, state.p - delta)
        # Move to MRU in B2
        del state.B2[key]
        state.B2[key] = None

    # 2. Determine victim
    # Replace(x) logic
    t1_excess = len(state.T1) > state.p
    in_b2_cond = (key in state.B2) and (len(state.T1) == int(state.p))

    if t1_excess or in_b2_cond:
        # Evict from T1 (LRU)
        if state.T1:
            return next(iter(state.T1))
        if state.T2: return next(iter(state.T2))
    else:
        # Evict from T2 (LRU)
        if state.T2:
            return next(iter(state.T2))
        if state.T1: return next(iter(state.T1))

    # Fallback
    if cache_snapshot.cache:
        return next(iter(cache_snapshot.cache))
    return None

def update_after_hit(cache_snapshot, obj):
    '''
    ARC: Hit in T1 -> move to T2. Hit in T2 -> MRU T2.
    '''
    state = get_state(cache_snapshot)
    key = obj.key

    if key in state.T1:
        del state.T1[key]
        state.T2[key] = None
    elif key in state.T2:
        del state.T2[key]
        state.T2[key] = None

def update_after_insert(cache_snapshot, obj):
    '''
    ARC: Insert to T1 (if new) or T2 (if in ghosts).
    '''
    state = get_state(cache_snapshot)
    key = obj.key

    is_ghost = False
    if key in state.B1:
        del state.B1[key]
        is_ghost = True
    if key in state.B2:
        del state.B2[key]
        is_ghost = True

    if is_ghost:
        state.T2[key] = None
    else:
        state.T1[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    ARC: Move evicted to B1/B2 and trim ghosts.
    '''
    state = get_state(cache_snapshot)
    key = evicted_obj.key

    # Identify source
    if key in state.T1:
        del state.T1[key]
        state.B1[key] = None # Add to MRU B1
    elif key in state.T2:
        del state.T2[key]
        state.B2[key] = None # Add to MRU B2
    else:
        # Fallback sync
        state.B1[key] = None

    # Enforce ghost capacity (simplification: |B1| <= c, |B2| <= c)
    c = cache_snapshot.capacity
    while len(state.B1) > c:
        del state.B1[next(iter(state.B1))]
    while len(state.B2) > c:
        del state.B2[next(iter(state.B2))]
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