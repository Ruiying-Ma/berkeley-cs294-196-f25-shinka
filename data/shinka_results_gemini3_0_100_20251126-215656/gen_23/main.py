# EVOLVE-BLOCK-START
"""S3-FIFO with Frequency Decay and Enlarged Ghost Queue"""

# Global state
# Dictionaries act as Ordered Sets (FIFO)
_s3_small = {}
_s3_main = {}
_s3_ghost = {}
_s3_freq = {}
_last_ts = -1

# Constants
_SMALL_RATIO = 0.1
_GHOST_RATIO = 20.0  # Large history to capture long loops

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
    S3-FIFO eviction with Frequency Decay and Ghost Queue.
    Strategy:
    - Main Queue: Protected. Uses 2-bit clock (Decay). Demotes to Small.
    - Small Queue: Probationary. FIFO. Promotes to Main on hit.
    - Ghost Queue: Tracks history of evicted items.
    '''
    global _s3_small, _s3_main, _s3_freq
    _check_reset(cache_snapshot)

    # Adaptive target for small queue could be used, but static 10% is robust.
    curr_size = len(cache_snapshot.cache)
    s_target = max(1, int(curr_size * _SMALL_RATIO))

    while True:
        # 1. Process Small Queue (Probation)
        # We prefer evicting from Small if it's larger than target.
        if len(_s3_small) > s_target:
            if not _s3_small:
                # Should not be empty if len > s_target, but safety break
                break
            
            candidate = next(iter(_s3_small))

            # Lazy consistency check
            if candidate not in cache_snapshot.cache:
                _s3_small.pop(candidate, None)
                _s3_freq.pop(candidate, None)
                continue

            freq = _s3_freq.get(candidate, 0)
            if freq > 0:
                # Lazy Promotion: Accessed while in Small -> Move to Main
                _s3_small.pop(candidate)
                _s3_main[candidate] = None # Append to tail (MRU)
                _s3_freq[candidate] = 0    # Reset freq after promotion
                continue
            else:
                # No access in Small -> Evict
                # We return the candidate. update_after_evict will handle Ghost insertion.
                return candidate

        # 2. Process Main Queue (Protected)
        # Only process Main if Small is within target size
        if _s3_main:
            candidate = next(iter(_s3_main))

            if candidate not in cache_snapshot.cache:
                _s3_main.pop(candidate, None)
                _s3_freq.pop(candidate, None)
                continue

            freq = _s3_freq.get(candidate, 0)
            if freq > 0:
                # Reinsertion with Decay
                # Decrement frequency instead of hard reset to 0.
                # Allows popular items (freq=3) to survive multiple passes.
                _s3_main.pop(candidate)
                _s3_main[candidate] = None # Reinsert at tail
                _s3_freq[candidate] = freq - 1
                continue
            else:
                # Demotion: Main -> Small
                # Item was not accessed (or freq decayed to 0).
                # Move to Small for one last probationary FIFO pass.
                _s3_main.pop(candidate)
                _s3_small[candidate] = None
                _s3_freq[candidate] = 0
                continue

        # 3. Fallback
        # If Main is empty or logic falls through, force evict from Small
        if _s3_small:
            candidate = next(iter(_s3_small))
            return candidate
            
        # Final fallback if local structures are empty but cache is full
        if cache_snapshot.cache:
            return next(iter(cache_snapshot.cache))
        return None

def update_after_hit(cache_snapshot, obj):
    global _s3_freq
    _check_reset(cache_snapshot)
    # Track frequency up to 3 (2 bits)
    current = _s3_freq.get(obj.key, 0)
    _s3_freq[obj.key] = min(current + 1, 3)

def update_after_insert(cache_snapshot, obj):
    global _s3_small, _s3_main, _s3_ghost, _s3_freq
    _check_reset(cache_snapshot)

    key = obj.key
    
    # Ghost Hit -> Promote to Main
    if key in _s3_ghost:
        if key not in _s3_main and key not in _s3_small:
            _s3_main[key] = None
            _s3_freq[key] = 0
        _s3_ghost.pop(key)
    else:
        # Standard Insert -> Small
        if key not in _s3_small and key not in _s3_main:
            _s3_small[key] = None
            _s3_freq[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    global _s3_small, _s3_main, _s3_ghost, _s3_freq
    _check_reset(cache_snapshot)

    key = evicted_obj.key

    # Manage Ghost Queue
    if key in _s3_small:
        _s3_small.pop(key)
        _s3_ghost[key] = None
        
        # Enforce Ghost Capacity
        if len(_s3_ghost) > cache_snapshot.capacity * _GHOST_RATIO:
            _s3_ghost.pop(next(iter(_s3_ghost)), None)
            
    elif key in _s3_main:
        _s3_main.pop(key)
        # Items evicted directly from Main (rare) don't go to Ghost in this logic,
        # but Demotion usually handles it.

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