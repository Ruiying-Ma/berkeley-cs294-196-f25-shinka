# EVOLVE-BLOCK-START
from collections import OrderedDict

# Global structures
# _q_small: FIFO queue for probation (Keys -> None)
# _q_main: LRU queue for protected items (Keys -> None)
# _q_ghost: FIFO queue for history (Keys -> None)
# _freq: Dictionary mapping key -> frequency count
_q_small = OrderedDict()
_q_main = OrderedDict()
_q_ghost = OrderedDict()
_freq = {}
_last_ts = -1

def _reset(snapshot):
    """Resets internal state if a new trace is detected."""
    global _q_small, _q_main, _q_ghost, _freq, _last_ts
    if snapshot.access_count < _last_ts:
        _q_small.clear()
        _q_main.clear()
        _q_ghost.clear()
        _freq.clear()
    
    # Safety clear if cache is empty but we have data
    if not snapshot.cache and (_q_small or _q_main):
        _q_small.clear()
        _q_main.clear()
        _q_ghost.clear()
        _freq.clear()
        
    _last_ts = snapshot.access_count

def evict(cache_snapshot, obj):
    '''
    S3-LRU-D2: S3-FIFO structure with LRU Main, Demotion, and Expanded Ghost.
    '''
    global _q_small, _q_main, _q_ghost, _freq
    _reset(cache_snapshot)
    
    capacity = cache_snapshot.capacity
    # Target size for Small queue (10%)
    s_target = max(1, int(capacity * 0.1))
    # Expanded history to capture larger loops
    ghost_limit = capacity * 4
    
    # Loop limit to prevent potential infinite loops during state transitions
    loop_limit = len(_q_small) + len(_q_main) + 10
    loops = 0
    
    while loops < loop_limit:
        loops += 1
        
        # Policy: Evict from Small if over target or Main is empty
        if len(_q_small) > s_target or not _q_main:
            if not _q_small:
                # Fallback to Main if Small is unexpectedly empty
                if _q_main:
                     cand, _ = _q_main.popitem(last=False)
                     _freq.pop(cand, None)
                     # Track in ghost
                     _q_ghost[cand] = None
                     if len(_q_ghost) > ghost_limit: _q_ghost.popitem(last=False)
                     return cand
                return obj.key

            # Check Small Head (FIFO)
            cand, _ = _q_small.popitem(last=False)
            
            # Consistency check
            if cand not in cache_snapshot.cache:
                _freq.pop(cand, None)
                continue

            if _freq.get(cand, 0) > 0:
                # Promote to Main (MRU)
                _q_main[cand] = None
                _freq[cand] = 0 # Reset freq
                continue
            else:
                # Evict from Small -> Ghost
                _q_ghost[cand] = None
                if len(_q_ghost) > ghost_limit:
                    _q_ghost.popitem(last=False)
                _freq.pop(cand, None)
                return cand
        
        else:
            # Policy: Evict from Main (LRU)
            cand, _ = _q_main.popitem(last=False)
            
            # Consistency check
            if cand not in cache_snapshot.cache:
                _freq.pop(cand, None)
                continue
                
            if _freq.get(cand, 0) > 0:
                # Reinsert in Main (Second Chance) -> Move to MRU
                _q_main[cand] = None 
                _freq[cand] = 0 # Reset freq for next cycle
                continue
            else:
                # Demote to Small (Probation) -> Move to Small Tail
                _q_small[cand] = None 
                _freq[cand] = 0
                continue
                
    # Fallback
    return next(iter(cache_snapshot.cache))

def update_after_hit(cache_snapshot, obj):
    global _q_small, _q_main, _freq
    _reset(cache_snapshot)
    
    key = obj.key
    # Cap freq to avoid unbounded growth
    _freq[key] = min(_freq.get(key, 0) + 1, 3)
    
    # If in Main, move to MRU (LRU behavior)
    if key in _q_main:
        _q_main.move_to_end(key)

def update_after_insert(cache_snapshot, obj):
    global _q_small, _q_main, _q_ghost, _freq
    _reset(cache_snapshot)
    
    key = obj.key
    _freq[key] = 0
    
    if key in _q_ghost:
        # Ghost Hit -> Promote to Main directly
        del _q_ghost[key]
        _q_main[key] = None
    else:
        # New -> Insert into Small (Probation)
        _q_small[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    global _q_small, _q_main, _freq
    _reset(cache_snapshot)
    
    key = evicted_obj.key
    # Cleanup internal state
    _q_small.pop(key, None)
    _q_main.pop(key, None)
    _freq.pop(key, None)
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