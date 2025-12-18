# EVOLVE-BLOCK-START
"""S3-FIFO with Decay and Always-Demote"""

# Global State
_s3_small = {}
_s3_main = {}
_s3_ghost = {}
_s3_freq = {}
_last_ts = -1

# Constants
_SMALL_RATIO = 0.1

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
    S3-FIFO with Always-Demote and Frequency Decay.
    - Small (Probation): FIFO. Hits promote to Main. Cold evicts to Ghost.
    - Main (Protected): FIFO. Hits reinsert with Decay. Cold DEMOTES to Small.
    '''
    global _s3_small, _s3_main, _s3_ghost, _s3_freq
    _check_reset(cache_snapshot)
    
    capacity = cache_snapshot.capacity
    s_target = max(1, int(capacity * _SMALL_RATIO))
    
    while True:
        # 1. Prefer evicting from Small if it exceeds target OR Main is empty
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
                _s3_freq[candidate] = 0
                continue
            else:
                # Eviction: Small -> Ghost
                _s3_small.pop(candidate)
                _s3_ghost[candidate] = None
                
                # Ghost capacity management
                if len(_s3_ghost) > capacity:
                    _s3_ghost.pop(next(iter(_s3_ghost)), None)
                
                _s3_freq.pop(candidate, None)
                return candidate
        
        # 2. Process Main
        else:
            if not _s3_main:
                 continue

            candidate = next(iter(_s3_main))
            
            if candidate not in cache_snapshot.cache:
                _s3_main.pop(candidate, None)
                _s3_freq.pop(candidate, None)
                continue
            
            freq = _s3_freq.get(candidate, 0)
            if freq > 0:
                # Reinsertion: Main -> Main (Decay)
                _s3_main.pop(candidate)
                _s3_main[candidate] = None
                _s3_freq[candidate] = freq - 1
                continue
            else:
                # Demotion: Main -> Small (Always)
                # Gives item a second chance in probation
                _s3_main.pop(candidate)
                _s3_small[candidate] = None
                _s3_freq[candidate] = 0
                continue

def update_after_hit(cache_snapshot, obj):
    global _s3_freq
    _check_reset(cache_snapshot)
    curr = _s3_freq.get(obj.key, 0)
    _s3_freq[obj.key] = min(curr + 1, 3)

def update_after_insert(cache_snapshot, obj):
    global _s3_small, _s3_main, _s3_ghost, _s3_freq
    _check_reset(cache_snapshot)
    
    key = obj.key
    # Ghost check for protection/promotion
    if key in _s3_ghost:
        if key not in _s3_main and key not in _s3_small:
            _s3_main[key] = None
            # Give it some frequency buffer (2) to stick in Main
            _s3_freq[key] = 2
        _s3_ghost.pop(key, None)
    else:
        # Standard insert to Small
        if key not in _s3_small and key not in _s3_main:
            _s3_small[key] = None
            _s3_freq[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    global _s3_small, _s3_main, _s3_freq
    _check_reset(cache_snapshot)
    
    key = evicted_obj.key
    # Cleanup (primary logic is in evict)
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