# EVOLVE-BLOCK-START
"""
S3-FIFO with Frequency-Based Retention and Extended Ghost (S3-FIFO-F-G2).
Combines:
1. S3-FIFO structure (Small/Main queues) with Conditional Demotion.
2. Frequency tracking (0-3) from the Current program for better Main retention (Scan resistance + Loop capture).
3. Extended Ghost Registry (2x capacity) from the Inspiration program for detecting long loops (Trace 29).
4. Conservative Rescue (rescued items start cold) to prevent pollution from the large ghost registry.
"""

from collections import OrderedDict

# Global structures
# _q_small: FIFO queue for probation (Keys -> None)
# _q_main: FIFO queue for protected items (Keys -> None)
# _q_ghost: FIFO queue for tracking history (Keys -> None)
# _freq_map: Dictionary mapping key -> frequency count (0-3)
_q_small = OrderedDict()
_q_main = OrderedDict()
_q_ghost = OrderedDict()
_freq_map = {}
_last_access_count = -1

def _reset_state_if_needed(snapshot):
    """Resets internal state if a new trace is detected via timestamp jump."""
    global _q_small, _q_main, _q_ghost, _freq_map, _last_access_count
    if snapshot.access_count < _last_access_count:
        _q_small.clear()
        _q_main.clear()
        _q_ghost.clear()
        _freq_map.clear()
    _last_access_count = snapshot.access_count

def evict(cache_snapshot, obj):
    global _q_small, _q_main, _q_ghost, _freq_map
    _reset_state_if_needed(cache_snapshot)
    
    capacity = cache_snapshot.capacity
    # Target size for small queue (10% of capacity, min 1)
    s_target = max(1, int(capacity * 0.1))
    
    # Safety fallback for empty/desync cache
    if not _q_small and not _q_main:
        return next(iter(cache_snapshot.cache)) if cache_snapshot.cache else None

    while True:
        # 1. Check Small Queue (Probation)
        # Priority to evict from Small if it exceeds target OR Main is empty
        if len(_q_small) > s_target or not _q_main:
            if not _q_small:
                # Should generally not be reached unless Main is also empty
                if _q_main:
                    candidate, _ = _q_main.popitem(last=False)
                    _freq_map.pop(candidate, None)
                    return candidate
                return next(iter(cache_snapshot.cache))

            candidate, _ = _q_small.popitem(last=False)
            
            # Sync check: ensure candidate is in cache
            if candidate not in cache_snapshot.cache:
                _freq_map.pop(candidate, None)
                continue

            freq = _freq_map.get(candidate, 0)
            if freq > 0:
                # Promotion: Small -> Main
                # Reset freq to 0. It must be hit in Main to gain "Main rights" (freq > 0)
                _q_main[candidate] = None
                _freq_map[candidate] = 0 
                continue
            else:
                # Eviction: Small -> Ghost
                _q_ghost[candidate] = None
                # Extended Ghost Size: 2x Capacity (Trace 29 optimization)
                if len(_q_ghost) > capacity * 2:
                    _q_ghost.popitem(last=False)
                _freq_map.pop(candidate, None)
                return candidate
        
        # 2. Check Main Queue (Protected)
        else:
            candidate, _ = _q_main.popitem(last=False)
            
            # Sync check
            if candidate not in cache_snapshot.cache:
                _freq_map.pop(candidate, None)
                continue
                
            freq = _freq_map.get(candidate, 0)
            if freq > 0:
                # Reinsertion: Main -> Main (Second Chance with Decay)
                _q_main[candidate] = None
                _freq_map[candidate] = freq - 1
                continue
            else:
                # Conditional Demotion
                # Demote to Small only if Small is not full
                if len(_q_small) < s_target:
                    _q_small[candidate] = None
                    _freq_map[candidate] = 0
                    continue
                else:
                    # Eviction: Main -> Ghost
                    _q_ghost[candidate] = None
                    if len(_q_ghost) > capacity * 2:
                        _q_ghost.popitem(last=False)
                    _freq_map.pop(candidate, None)
                    return candidate

def update_after_hit(cache_snapshot, obj):
    global _freq_map
    _reset_state_if_needed(cache_snapshot)
    
    # Increment frequency, cap at 3 (from Current program)
    curr = _freq_map.get(obj.key, 0)
    _freq_map[obj.key] = min(curr + 1, 3)

def update_after_insert(cache_snapshot, obj):
    global _q_small, _q_main, _q_ghost, _freq_map
    _reset_state_if_needed(cache_snapshot)
    
    key = obj.key
    
    if key in _q_ghost:
        # Rescue: Ghost -> Main
        del _q_ghost[key]
        _q_main[key] = None
        # Conservative Rescue: freq=0. 
        # Prevents pollution from large ghost. Must prove utility again.
        _freq_map[key] = 0
    else:
        # Insert: -> Small
        _q_small[key] = None
        _freq_map[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    global _q_small, _q_main, _freq_map
    _reset_state_if_needed(cache_snapshot)
    
    key = evicted_obj.key
    # Cleanup
    if key in _q_small: del _q_small[key]
    if key in _q_main: del _q_main[key]
    _freq_map.pop(key, None)
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