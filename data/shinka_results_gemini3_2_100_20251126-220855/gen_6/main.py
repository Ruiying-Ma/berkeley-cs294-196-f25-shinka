# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

# Segmented LRU (SLRU) Metadata
# slru_probation: dict for probationary segment (ordered by access/insertion)
# slru_protected: dict for protected segment (ordered by access)
slru_probation = {}
slru_protected = {}

def evict(cache_snapshot, obj):
    '''
    This function defines how the algorithm chooses the eviction victim.
    SLRU Strategy: Evict from Probation (LRU) first. If empty, evict from Protected (LRU).
    '''
    global slru_probation, slru_protected

    # Check consistency: if global state is misaligned with cache_snapshot (e.g. due to restart), clean up
    # However, iterating to clean up is costly. We assume consistent environment or handle missing keys gracefully.

    # Priority 1: Evict from Probation
    if slru_probation:
        # The dictionary preserves insertion order.
        # Since we re-insert on access (in update_after_hit/insert),
        # the first item is the Least Recently Used/Inserted.
        return next(iter(slru_probation))

    # Priority 2: Evict from Protected
    if slru_protected:
        return next(iter(slru_protected))

    # Fallback if both empty (should imply cache is empty, but evict called means full?)
    if cache_snapshot.cache:
        return next(iter(cache_snapshot.cache))
    return None

def update_after_hit(cache_snapshot, obj):
    '''
    SLRU Update on Hit:
    - If in Probation: Promote to Protected. Enforce Protected capacity.
    - If in Protected: Update LRU (move to MRU).
    '''
    global slru_probation, slru_protected

    key = obj.key
    if key in slru_probation:
        # Remove from Probation
        slru_probation.pop(key)
        # Add to Protected (MRU)
        slru_protected[key] = None

        # Enforce Protected Capacity (e.g., 80% of total capacity)
        # Using 80% is a common heuristic for SLRU
        protected_limit = int(cache_snapshot.capacity * 0.8)
        if len(slru_protected) > protected_limit:
            # Demote LRU of Protected to Probation (MRU)
            demoted_key = next(iter(slru_protected))
            slru_protected.pop(demoted_key)
            slru_probation[demoted_key] = None

    elif key in slru_protected:
        # Move to MRU in Protected
        slru_protected.pop(key)
        slru_protected[key] = None

    else:
        # Consistency recovery: if hit but not in our records, treat as inserted in probation?
        # Or maybe it was just inserted?
        # For safety, add to probation if not present.
        slru_probation[key] = None

def update_after_insert(cache_snapshot, obj):
    '''
    SLRU Update on Insert:
    - New objects go to Probation (MRU).
    '''
    global slru_probation, slru_protected
    slru_probation[obj.key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    SLRU Update on Evict:
    - Remove evicted object from metadata.
    '''
    global slru_probation, slru_protected
    key = evicted_obj.key
    if key in slru_probation:
        slru_probation.pop(key)
    elif key in slru_protected:
        slru_protected.pop(key)

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