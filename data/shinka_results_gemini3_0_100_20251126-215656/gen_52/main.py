# EVOLVE-BLOCK-START
"""S3-FIFO with Ghost Queue (S3-FIFO-D style)"""

# Global state
# We use dicts as ordered sets (insertion order preserved)
_s3_small = {}
_s3_main = {}
_s3_ghost = {} # Ghost queue for tracking evicted small objects
_s3_freq = {}
_last_ts = -1

def _check_reset(snapshot):
    global _s3_small, _s3_main, _s3_ghost, _s3_freq, _last_ts
    # If access count drops, it indicates a new trace has started
    if snapshot.access_count < _last_ts:
        _s3_small.clear()
        _s3_main.clear()
        _s3_ghost.clear()
        _s3_freq.clear()
    _last_ts = snapshot.access_count

def evict(cache_snapshot, obj):
    '''
    S3-FIFO eviction with Ghost Queue support and Main->Small Demotion.
    Evicts from Small (Probation) or Main (Protected).
    '''
    global _s3_small, _s3_main, _s3_freq
    _check_reset(cache_snapshot)

    # Target size for small queue (10% of capacity)
    curr_size = len(cache_snapshot.cache)
    s_target = max(1, int(curr_size * 0.1))

    while True:
        # 1. Check Small FIFO (Probation)
        # If Small is larger than target, we prefer to evict from it.
        # This handles both new items and demoted items from Main.
        if len(_s3_small) > s_target:
            if not _s3_small:
                break

            candidate = next(iter(_s3_small))

            # Consistency check
            if candidate not in cache_snapshot.cache:
                _s3_small.pop(candidate, None)
                _s3_freq.pop(candidate, None)
                continue

            # S3-FIFO Logic:
            freq = _s3_freq.get(candidate, 0)
            if freq > 0:
                # Accessed while in Small -> Promote to Main
                _s3_small.pop(candidate)
                _s3_main[candidate] = None # Insert at tail (MRU)
                _s3_freq[candidate] = 0    # Reset frequency
                continue
            else:
                # Not visited: Evict from Small
                return candidate

        # 2. Check Main FIFO (Protected)
        # We process Main if Small is small enough.
        if _s3_main:
            candidate = next(iter(_s3_main))

            if candidate not in cache_snapshot.cache:
                _s3_main.pop(candidate, None)
                _s3_freq.pop(candidate, None)
                continue

            freq = _s3_freq.get(candidate, 0)
            if freq > 0:
                # Accessed while in Main -> Second Chance (Reinsert)
                _s3_main.pop(candidate)
                _s3_main[candidate] = None # Reinsert at tail
                _s3_freq[candidate] = 0
                continue
            else:
                # Not visited in Main: Conditional Demotion
                # Only demote to Small if Small is not overflowing.
                # This protects Small from being flooded by cold Main items during scans.
                if len(_s3_small) < s_target:
                    _s3_main.pop(candidate)
                    _s3_small[candidate] = None # Insert at tail of Small
                    _s3_freq[candidate] = 0
                    continue
                else:
                    # Small is full/overflowing: Evict directly from Main
                    # This item will be added to Ghost in update_after_evict
                    return candidate

        # 3. Fallback: Main is empty? Check Small regardless of size
        if not _s3_main and _s3_small:
            candidate = next(iter(_s3_small))
            if candidate not in cache_snapshot.cache:
                _s3_small.pop(candidate, None)
                _s3_freq.pop(candidate, None)
                continue

            freq = _s3_freq.get(candidate, 0)
            if freq > 0:
                 _s3_small.pop(candidate)
                 _s3_main[candidate] = None
                 _s3_freq[candidate] = 0
                 continue
            else:
                 return candidate

        # If both empty (should not happen on full cache)
        if not _s3_small and not _s3_main:
            if cache_snapshot.cache:
                return next(iter(cache_snapshot.cache))
            return None

def update_after_hit(cache_snapshot, obj):
    global _s3_freq
    _check_reset(cache_snapshot)
    # Cap frequency at 3
    current = _s3_freq.get(obj.key, 0)
    _s3_freq[obj.key] = min(current + 1, 3)

def update_after_insert(cache_snapshot, obj):
    global _s3_small, _s3_main, _s3_ghost, _s3_freq
    _check_reset(cache_snapshot)

    key = obj.key
    # S3-FIFO with Ghost:
    # If in ghost, it means it was evicted from Small recently.
    # Promote directly to Main to avoid "scan" classification.
    if key in _s3_ghost:
        if key not in _s3_main and key not in _s3_small:
            _s3_main[key] = None
            _s3_freq[key] = 0
        _s3_ghost.pop(key)
    else:
        # Standard insert to Small (Probation)
        if key not in _s3_small and key not in _s3_main:
            _s3_small[key] = None
            _s3_freq[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    global _s3_small, _s3_main, _s3_ghost, _s3_freq
    _check_reset(cache_snapshot)

    key = evicted_obj.key

    # Track eviction in Ghost regardless of source queue
    # This helps recovery of both scan items (Small) and working set items (Main)
    if key in _s3_small:
        _s3_small.pop(key)
    elif key in _s3_main:
        _s3_main.pop(key)

    # Add to Ghost
    _s3_ghost[key] = None

    # Limit ghost size
    # Increased to 4x capacity to capture even larger loops/patterns
    if len(_s3_ghost) > cache_snapshot.capacity * 4:
        _s3_ghost.pop(next(iter(_s3_ghost)), None)

    _s3_freq.pop(key, None)
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