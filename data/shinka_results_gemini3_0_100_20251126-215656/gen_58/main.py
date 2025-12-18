# EVOLVE-BLOCK-START
from collections import OrderedDict

# S3-FIFO-D-G (Dynamic Demotion + Ghost Origins)
# - q_small: FIFO queue for new/probationary items
# - q_main: FIFO queue for protected items
# - q_ghost: FIFO queue mapping key -> origin_was_main (bool)
# - meta_freq: Frequency counter (0-3)

q_small = OrderedDict()
q_main = OrderedDict()
q_ghost = OrderedDict()
meta_freq = {}
last_chk_time = -1

def _maintain_consistency(snapshot):
    global last_chk_time, q_small, q_main, q_ghost, meta_freq
    # Reset if time moves backward or cache is empty but state exists
    if snapshot.access_count < last_chk_time or (not snapshot.cache and (q_small or q_main)):
        q_small.clear()
        q_main.clear()
        q_ghost.clear()
        meta_freq.clear()
    last_chk_time = snapshot.access_count

def evict(cache_snapshot, obj):
    '''
    S3-FIFO eviction with:
    - Conditional M->S demotion (only if S has space)
    - Origin-aware Ghost management
    '''
    global q_small, q_main, q_ghost, meta_freq
    _maintain_consistency(cache_snapshot)
    
    capacity = cache_snapshot.capacity
    # Target Small queue size (10% of capacity)
    target_s = max(1, int(capacity * 0.1))
    
    # Operations limit to prevent infinite loops in extreme cases
    ops = 0
    limit = len(cache_snapshot.cache) * 4
    
    while ops < limit:
        ops += 1
        
        # S3-FIFO Policy: 
        # Scan Small if it's large or Main is empty. Otherwise scan Main.
        check_small = False
        if len(q_small) > target_s or not q_main:
            check_small = True
            
        if check_small:
            if not q_small:
                # If S is empty, try M (unless M is also empty)
                if q_main:
                    check_small = False
                else:
                    break # Cache is empty
            
            if check_small:
                # Process Small Queue
                cand, _ = q_small.popitem(last=False)
                
                # Cleanup if inconsistent
                if cand not in cache_snapshot.cache:
                    meta_freq.pop(cand, None)
                    continue
                
                freq = meta_freq.get(cand, 0)
                if freq > 0:
                    # Promote to Main
                    q_main[cand] = None
                    meta_freq[cand] = 0 # Reset frequency
                    continue
                else:
                    # Evict from Small
                    # Record origin as Small (False)
                    q_ghost[cand] = False
                    if len(q_ghost) > capacity * 3:
                        q_ghost.popitem(last=False)
                    
                    meta_freq.pop(cand, None)
                    return cand
        else:
            # Process Main Queue
            cand, _ = q_main.popitem(last=False)
            
            if cand not in cache_snapshot.cache:
                meta_freq.pop(cand, None)
                continue
                
            freq = meta_freq.get(cand, 0)
            if freq > 0:
                # Reinsert to Main with decay
                q_main[cand] = None
                meta_freq[cand] = freq - 1
                continue
            else:
                # Conditional Demotion
                # Only demote to S if S is not full.
                # This prevents cold M items from trashing S during scans.
                if len(q_small) < target_s:
                    q_small[cand] = None
                    meta_freq[cand] = 0
                    continue
                else:
                    # Direct Eviction from Main (bypass S)
                    # Record origin as Main (True)
                    q_ghost[cand] = True
                    if len(q_ghost) > capacity * 3:
                        q_ghost.popitem(last=False)
                    
                    meta_freq.pop(cand, None)
                    return cand

    # Fallback
    if cache_snapshot.cache:
        return next(iter(cache_snapshot.cache))
    return None

def update_after_hit(cache_snapshot, obj):
    '''Increment frequency on hit, capped at 3.'''
    global meta_freq
    _maintain_consistency(cache_snapshot)
    k = obj.key
    meta_freq[k] = min(meta_freq.get(k, 0) + 1, 3)

def update_after_insert(cache_snapshot, obj):
    '''Handle new insertions and ghost hits.'''
    global q_small, q_main, q_ghost, meta_freq
    _maintain_consistency(cache_snapshot)
    
    k = obj.key
    
    if k in q_ghost:
        origin_main = q_ghost.pop(k)
        if origin_main:
            # Ghost from Main: Restore directly to Main
            q_main[k] = None
            meta_freq[k] = 0
        else:
            # Ghost from Small: Restore to Small
            # Initialize with freq=1 to ensure promotion on next scan
            q_small[k] = None
            meta_freq[k] = 1
    else:
        # New Insert: Start in Small with 0 frequency
        q_small[k] = None
        meta_freq[k] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''Cleanup metadata.'''
    global q_small, q_main, meta_freq
    _maintain_consistency(cache_snapshot)
    
    k = evicted_obj.key
    q_small.pop(k, None)
    q_main.pop(k, None)
    meta_freq.pop(k, None)
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