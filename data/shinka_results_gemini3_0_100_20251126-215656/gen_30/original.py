# EVOLVE-BLOCK-START
from collections import OrderedDict

# ARC Algorithm Implementation
# Global state for ARC (T1, T2, B1, B2, p)
# T1: Recent (LRU), T2: Frequent (LRU), B1: Ghost Recent, B2: Ghost Frequent
arc_t1 = OrderedDict()
arc_t2 = OrderedDict()
arc_b1 = OrderedDict()
arc_b2 = OrderedDict()
arc_p = 0
arc_last_access_count = -1

def _check_reset(cache_snapshot):
    """
    Detects if a new trace has started or if state needs reset.
    """
    global arc_t1, arc_t2, arc_b1, arc_b2, arc_p, arc_last_access_count
    current_acc = cache_snapshot.access_count
    
    # If access count dropped, it's a new trace
    if current_acc < arc_last_access_count:
        arc_t1.clear()
        arc_t2.clear()
        arc_b1.clear()
        arc_b2.clear()
        arc_p = 0
    
    # Safety reset if cache is empty but we have state (e.g. initial run)
    if len(cache_snapshot.cache) == 0 and (len(arc_t1) > 0 or len(arc_t2) > 0):
        arc_t1.clear()
        arc_t2.clear()
        arc_b1.clear()
        arc_b2.clear()
        arc_p = 0
        
    arc_last_access_count = current_acc

def evict(cache_snapshot, obj):
    '''
    Selects the eviction victim using ARC logic (Adaptive Replacement Cache).
    '''
    _check_reset(cache_snapshot)
    global arc_p
    
    key = obj.key
    c = cache_snapshot.capacity
    
    # 1. Adapt p (target size for T1) if we have a ghost hit
    if key in arc_b1:
        delta = 1
        if len(arc_b1) >= len(arc_b2):
            delta = 1
        else:
            delta = len(arc_b2) / len(arc_b1)
        arc_p = min(float(c), arc_p + delta)
    elif key in arc_b2:
        delta = 1
        if len(arc_b2) >= len(arc_b1):
            delta = 1
        else:
            delta = len(arc_b1) / len(arc_b2)
        arc_p = max(0.0, arc_p - delta)

    # 2. Determine victim using ARC's REPLACE logic
    victim_key = None
    t1_len = len(arc_t1)
    replace_p = arc_p
    
    evict_from_t1 = False
    if t1_len > 0:
        if t1_len > replace_p:
            evict_from_t1 = True
        elif (key in arc_b2) and (t1_len == int(replace_p)):
            evict_from_t1 = True
    
    if evict_from_t1:
        victim_key = next(iter(arc_t1))
    else:
        if len(arc_t2) > 0:
            victim_key = next(iter(arc_t2))
        else:
            # Fallback: if T2 is empty but we decided to evict from it (rare), evict T1
            if len(arc_t1) > 0:
                victim_key = next(iter(arc_t1))
    
    # Safety fallback: ensure victim is actually in the cache snapshot
    if victim_key is None or victim_key not in cache_snapshot.cache:
        # Just pick LRU from the actual cache as a failsafe
        if cache_snapshot.cache:
            victim_key = next(iter(cache_snapshot.cache))
            
    return victim_key

def update_after_hit(cache_snapshot, obj):
    '''
    Updates ARC state on cache hit.
    '''
    _check_reset(cache_snapshot)
    key = obj.key
    
    # Move to T2 MRU
    if key in arc_t1:
        del arc_t1[key]
        arc_t2[key] = None
    elif key in arc_t2:
        arc_t2.move_to_end(key)
    else:
        # If strictly following logic, it should be in one. If not, add to T2.
        arc_t2[key] = None

    # Remove from ghosts if present
    if key in arc_b1: del arc_b1[key]
    if key in arc_b2: del arc_b2[key]

def update_after_insert(cache_snapshot, obj):
    '''
    Updates ARC state after insertion (miss).
    '''
    _check_reset(cache_snapshot)
    key = obj.key
    
    # Check ghost hits to determine placement
    if key in arc_b1:
        del arc_b1[key]
        arc_t2[key] = None # Promote to T2
    elif key in arc_b2:
        del arc_b2[key]
        arc_t2[key] = None # Promote to T2
    else:
        # New object -> T1
        arc_t1[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Updates ARC state after eviction (move to ghosts).
    '''
    _check_reset(cache_snapshot)
    key = evicted_obj.key
    c = cache_snapshot.capacity
    
    # Move evicted key to corresponding ghost list
    if key in arc_t1:
        del arc_t1[key]
        arc_b1[key] = None
        # Bound B1 size (heuristic: keep up to c)
        if len(arc_b1) > c:
            arc_b1.popitem(last=False) # remove LRU
            
    elif key in arc_t2:
        del arc_t2[key]
        arc_b2[key] = None
        # Bound B2 size (heuristic: keep up to 2c)
        if len(arc_b2) > 2*c:
            arc_b2.popitem(last=False) # remove LRU
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