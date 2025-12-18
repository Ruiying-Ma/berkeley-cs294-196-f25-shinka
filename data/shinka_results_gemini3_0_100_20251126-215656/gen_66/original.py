# EVOLVE-BLOCK-START
from collections import OrderedDict

# Global structures
# q_small: FIFO queue for probation (Keys -> None)
# q_main: LRU queue for protected items (Keys -> None)
# q_ghost: FIFO queue for tracking history (Keys -> None)
# meta_freq: Dictionary mapping key -> frequency count
q_small = OrderedDict()
q_main = OrderedDict()
q_ghost = OrderedDict()
meta_freq = {}

# Tracking trace changes
last_access_count = -1

def _reset_state(snapshot):
    """Resets internal state if a new trace is detected."""
    global q_small, q_main, q_ghost, meta_freq, last_access_count
    # Check for time travel (new trace) or empty cache reset
    if snapshot.access_count < last_access_count:
        q_small.clear()
        q_main.clear()
        q_ghost.clear()
        meta_freq.clear()
    
    # Consistency check: if cache is empty but we have data, clear it.
    if not snapshot.cache and (q_small or q_main):
        q_small.clear()
        q_main.clear()
        q_ghost.clear()
        meta_freq.clear()
        
    last_access_count = snapshot.access_count

def evict(cache_snapshot, obj):
    '''
    S3-FIFO Eviction Logic with Frequency Decay.
    '''
    global q_small, q_main, q_ghost, meta_freq
    _reset_state(cache_snapshot)
    
    capacity = cache_snapshot.capacity
    target_small = max(1, int(capacity * 0.1))
    
    # Safety mechanism to prevent infinite loops
    # In worst case, we cycle through all items in Small and Main once
    max_checks = len(q_small) + len(q_main) + 10
    checks = 0
    
    while checks < max_checks:
        checks += 1
        
        # Policy: Evict from Small if it's over budget or if Main is empty.
        # Otherwise, evict from Main.
        if len(q_small) > target_small or not q_main:
            # --- EVICT FROM SMALL (FIFO) ---
            if not q_small:
                # Should not happen unless Main is also empty
                if q_main:
                    return q_main.popitem(last=False)[0]
                return obj.key # Rejection (shouldn't happen)

            candidate, _ = q_small.popitem(last=False) # Head of FIFO
            
            freq = meta_freq.get(candidate, 0)
            if freq > 0:
                # Promote to Main
                q_main[candidate] = None
                meta_freq[candidate] = 0 # Reset frequency after promotion
                continue
            else:
                # Evict
                q_ghost[candidate] = None
                if len(q_ghost) > capacity:
                    q_ghost.popitem(last=False)
                meta_freq.pop(candidate, None)
                return candidate
        
        else:
            # --- EVICT FROM MAIN (LRU) ---
            candidate, _ = q_main.popitem(last=False) # Head of LRU (Oldest)
            
            freq = meta_freq.get(candidate, 0)
            if freq > 0:
                # Demote to Small (Second Chance)
                # DECAY logic: Halve frequency. 
                # If it remains > 0, it will pass through Small and re-promote if not evicted.
                # If it becomes 0, it needs a hit in Small to survive.
                new_freq = freq // 2
                meta_freq[candidate] = new_freq
                
                q_small[candidate] = None # Add to Tail of Small
                continue
            else:
                # Evict
                q_ghost[candidate] = None
                if len(q_ghost) > capacity:
                    q_ghost.popitem(last=False)
                meta_freq.pop(candidate, None)
                return candidate
                
    # Fallback
    if q_small: return q_small.popitem(last=False)[0]
    if q_main: return q_main.popitem(last=False)[0]
    return next(iter(cache_snapshot.cache))

def update_after_hit(cache_snapshot, obj):
    '''
    On Hit:
    - Increment frequency.
    - If in Main, move to MRU (Standard LRU behavior for Main).
    - If in Small, leave it (Lazy promotion handled at eviction).
    '''
    global q_small, q_main, meta_freq
    _reset_state(cache_snapshot)
    
    key = obj.key
    meta_freq[key] = meta_freq.get(key, 0) + 1
    
    if key in q_main:
        q_main.move_to_end(key)

def update_after_insert(cache_snapshot, obj):
    '''
    On Insert:
    - If Ghost Hit: Insert to Main (Recovers hot item).
    - Else: Insert to Small (Probation).
    '''
    global q_small, q_main, q_ghost, meta_freq
    _reset_state(cache_snapshot)
    
    key = obj.key
    # Initialize frequency
    meta_freq[key] = 0
    
    if key in q_ghost:
        # Ghost Hit -> Main
        del q_ghost[key]
        q_main[key] = None
    else:
        # New -> Small
        q_small[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Cleanup metadata.
    '''
    global q_small, q_main, meta_freq
    _reset_state(cache_snapshot)
    
    key = evicted_obj.key
    q_small.pop(key, None)
    q_main.pop(key, None)
    meta_freq.pop(key, None)
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