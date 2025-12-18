# EVOLVE-BLOCK-START
from collections import OrderedDict

# Global State
# _q_small: FIFO queue for probation items.
# _q_main: LRU queue for protected items.
# _q_ghost: FIFO queue for eviction history.
# _freq: Frequency counter for S3-FIFO logic.
_q_small = OrderedDict()
_q_main = OrderedDict()
_q_ghost = OrderedDict()
_freq = {}
_last_ts = -1

# Tuning Parameters
SMALL_RATIO = 0.20  # 20% for probation (Recommendation 5)
GHOST_RATIO = 4.0   # 4x history (Recommendation 2)
FREQ_CAP = 3        # Cap frequency to prevent integer overflow/stagnation

def _reset_if_new_trace(snapshot):
    """Resets state when a new trace is detected."""
    global _q_small, _q_main, _q_ghost, _freq, _last_ts
    if snapshot.access_count < _last_ts:
        _q_small.clear()
        _q_main.clear()
        _q_ghost.clear()
        _freq.clear()
    _last_ts = snapshot.access_count

def evict(cache_snapshot, obj):
    '''
    S3-FIFO based eviction with Main-Queue Demotion and Ghost Registry.
    - Filters scans in Small queue.
    - Protects working set in Main queue.
    - Recovers false negatives via Ghost queue.
    '''
    global _q_small, _q_main, _freq
    _reset_if_new_trace(cache_snapshot)
    
    capacity = cache_snapshot.capacity
    target_small = max(1, int(capacity * SMALL_RATIO))
    
    # Safety counter to prevent infinite loops in edge cases
    max_loops = (len(_q_small) + len(_q_main)) * 2 + 100
    loop_cnt = 0
    
    while loop_cnt < max_loops:
        loop_cnt += 1
        
        # Determine which queue to process
        # Prioritize Small if it exceeds its target size
        # Also process Small if Main is empty
        if len(_q_small) > target_small or not _q_main:
            if not _q_small:
                # Should be rare (Main must be non-empty here due to 'or')
                if _q_main:
                    return next(iter(_q_main))
                return None # Cache empty

            candidate = next(iter(_q_small))
            
            # Consistency check
            if candidate not in cache_snapshot.cache:
                _q_small.popitem(last=False)
                _freq.pop(candidate, None)
                continue
            
            # S3-FIFO Logic for Small (Probation)
            f = _freq.get(candidate, 0)
            if f > 0:
                # Hit in probation -> Promote to Main
                _q_small.move_to_end(candidate) # pop from head effectively
                _q_small.popitem(last=True)     # ...and remove
                _q_main[candidate] = None       # Insert at MRU of Main
                _freq[candidate] = 0            # Reset frequency
                continue
            else:
                # No hit -> Evict
                # We return the key; update_after_evict handles ghost insertion
                return candidate
        
        else:
            # Process Main (Protected)
            # Main is LRU, check the least recently used item
            candidate = next(iter(_q_main))
            
            if candidate not in cache_snapshot.cache:
                _q_main.popitem(last=False)
                _freq.pop(candidate, None)
                continue
                
            f = _freq.get(candidate, 0)
            if f > 0:
                # Accessed in Main -> Second Chance
                # Move to MRU, reset freq
                _q_main.move_to_end(candidate)
                _freq[candidate] = 0
                continue
            else:
                # Cold in Main
                # Recommendation 1: Capacity-Gated Conditional Demotion
                # Only demote to Small if Small is not full.
                # This prevents evicted Main items from displacing new items during scans.
                if len(_q_small) < target_small:
                    _q_main.popitem(last=False)
                    _q_small[candidate] = None # Add to tail of Small
                    _freq[candidate] = 0
                    continue
                else:
                    # Small is full -> Strict Eviction from Main
                    # This ensures we don't recycle trash when under pressure
                    return candidate

    # Fallback if loop limit reached
    if _q_small: return next(iter(_q_small))
    if _q_main: return next(iter(_q_main))
    return None

def update_after_hit(cache_snapshot, obj):
    global _q_main, _freq
    _reset_if_new_trace(cache_snapshot)
    
    key = obj.key
    curr = _freq.get(key, 0)
    _freq[key] = min(curr + 1, FREQ_CAP)
    
    # If in Main, update LRU position (MRU)
    if key in _q_main:
        _q_main.move_to_end(key)
    # If in Small, we don't move it (Lazy promotion handled in evict)

def update_after_insert(cache_snapshot, obj):
    global _q_small, _q_main, _q_ghost, _freq
    _reset_if_new_trace(cache_snapshot)
    
    key = obj.key
    # Default initial frequency
    _freq[key] = 0
    
    if key in _q_ghost:
        # Ghost Hit -> Promote to Main directly
        # Recommendation 4: Tiered Promotion Frequencies
        # Give a bonus freq to items recovering from Ghost
        del _q_ghost[key]
        _q_main[key] = None
        _freq[key] = 2 
    else:
        # Standard Insert -> Probation (Small)
        _q_small[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    global _q_small, _q_main, _q_ghost, _freq
    _reset_if_new_trace(cache_snapshot)
    
    key = evicted_obj.key
    
    # Remove from tracking queues
    if key in _q_small: del _q_small[key]
    if key in _q_main: del _q_main[key]
    _freq.pop(key, None)
    
    # Add to Ghost Registry
    _q_ghost[key] = None
    
    # Enforce Ghost Capacity
    # Recommendation 2: Extended Ghost Registry
    g_target = int(cache_snapshot.capacity * GHOST_RATIO)
    if len(_q_ghost) > g_target:
        _q_ghost.popitem(last=False) # FIFO eviction
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