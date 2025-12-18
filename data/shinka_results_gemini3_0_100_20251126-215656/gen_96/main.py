# EVOLVE-BLOCK-START
"""S3-FIFO with Conditional Demotion and Origin-Aware Ghost"""

# Global state
# _s3_small: Probation queue (FIFO)
# _s3_main: Protected queue (FIFO)
# _s3_ghost: Dictionary mapping key -> bool (True if evicted from Main, False if from Small)
# _s3_freq: Frequency counter for objects
_s3_small = {}
_s3_main = {}
_s3_ghost = {}
_s3_freq = {}
_last_ts = -1

def _check_reset(snapshot):
    global _s3_small, _s3_main, _s3_ghost, _s3_freq, _last_ts
    if snapshot.access_count < _last_ts:
        _s3_small.clear()
        _s3_main.clear()
        _s3_ghost.clear()
        _s3_freq.clear()
    _last_ts = snapshot.access_count

def evict(cache_snapshot, obj):
    '''
    S3-FIFO eviction with Conditional Demotion, Reduced Small Queue, and Extended Ghost.
    Strategy:
    - Small (Probation): Evict if > 5% capacity. Hits promote to Main.
    - Main (Protected): Evict if Small <= 5%. Hits reinsert.
    - Conditional Demotion: Main victims demote to Small ONLY if Small is not full.
    - Extended Ghost: Tracks evicted items (up to 5x capacity) to rescue looping patterns.
    '''
    global _s3_small, _s3_main, _s3_ghost, _s3_freq
    _check_reset(cache_snapshot)

    # Target size for small queue (5% of capacity, min 1)
    # Using capacity ensures stable threshold
    s_target = max(1, int(cache_snapshot.capacity * 0.05))

    while True:
        # 1. Check Small FIFO (Probation)
        # Priority to evict from Small if it exceeds target OR Main is empty
        if len(_s3_small) > s_target or not _s3_main:
            if not _s3_small:
                # Fallback if both empty
                if cache_snapshot.cache:
                    return next(iter(cache_snapshot.cache))
                return None

            candidate = next(iter(_s3_small))

            # Sync check
            if candidate not in cache_snapshot.cache:
                _s3_small.pop(candidate, None)
                _s3_freq.pop(candidate, None)
                continue

            freq = _s3_freq.get(candidate, 0)
            if freq > 0:
                # Promotion: Small -> Main
                _s3_small.pop(candidate)
                _s3_main[candidate] = None
                _s3_freq[candidate] = 0 # Reset freq on promotion
                continue
            else:
                # Eviction: Small -> Ghost
                _s3_small.pop(candidate)
                # Track in Ghost (False = from Small)
                _s3_ghost[candidate] = False
                _s3_freq.pop(candidate, None)

                # Extended Ghost capacity management (5x)
                if len(_s3_ghost) > cache_snapshot.capacity * 5:
                    _s3_ghost.pop(next(iter(_s3_ghost)), None)

                return candidate

        # 2. Check Main FIFO (Protected)
        else:
            candidate = next(iter(_s3_main))

            if candidate not in cache_snapshot.cache:
                _s3_main.pop(candidate, None)
                _s3_freq.pop(candidate, None)
                continue

            freq = _s3_freq.get(candidate, 0)
            if freq > 0:
                # Reinsertion: Main -> Main (Second Chance with Decay)
                _s3_main.pop(candidate)
                _s3_main[candidate] = None
                _s3_freq[candidate] = freq - 1
                continue
            else:
                # Conditional Demotion: Main -> Small
                # Only demote if Small is not full.
                if len(_s3_small) < s_target:
                    _s3_main.pop(candidate)
                    _s3_small[candidate] = None
                    _s3_freq[candidate] = 0
                    continue
                else:
                    # Eviction: Main -> Ghost
                    _s3_main.pop(candidate)
                    # Track in Ghost (True = from Main)
                    _s3_ghost[candidate] = True
                    _s3_freq.pop(candidate, None)

                    # Extended Ghost capacity management (5x)
                    if len(_s3_ghost) > cache_snapshot.capacity * 5:
                        _s3_ghost.pop(next(iter(_s3_ghost)), None)

                    return candidate

def update_after_hit(cache_snapshot, obj):
    global _s3_freq
    _check_reset(cache_snapshot)
    curr = _s3_freq.get(obj.key, 0)
    # Increased frequency ceiling to 7
    _s3_freq[obj.key] = min(curr + 1, 7)

def update_after_insert(cache_snapshot, obj):
    global _s3_small, _s3_main, _s3_ghost, _s3_freq
    _check_reset(cache_snapshot)

    key = obj.key
    # Ghost check
    if key in _s3_ghost:
        from_main = _s3_ghost.pop(key)

        if key not in _s3_main and key not in _s3_small:
            # Promote to Main
            _s3_main[key] = None
            # If it was from Main, give it a buffer (freq=1)
            # If from Small, it has to prove itself again immediately (freq=0)
            if from_main:
                _s3_freq[key] = 1
            else:
                _s3_freq[key] = 0
    else:
        # Standard insert to Small
        if key not in _s3_small and key not in _s3_main:
            _s3_small[key] = None
            _s3_freq[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    global _s3_small, _s3_main, _s3_freq
    _check_reset(cache_snapshot)

    key = evicted_obj.key
    # Cleanup (Ghost handling is primarily in evict, but this ensures consistency)
    _s3_small.pop(key, None)
    _s3_main.pop(key, None)
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