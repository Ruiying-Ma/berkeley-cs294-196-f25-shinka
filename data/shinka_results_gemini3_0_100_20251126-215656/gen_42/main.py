# EVOLVE-BLOCK-START
"""
S3-FIFO-P (Probationary)
Implements S3-FIFO with:
1. Capacity-Aware Conditional Demotion (Main -> Small only if space)
2. Two-Hit Promotion (Small -> Main requires 2 hits)
3. Origin-Based Ghost Handling (Restore to source queue)
4. Grace Frequency on Demotion (Give demoted items a chance)
"""

from collections import OrderedDict

# Global state
_s3_small = OrderedDict()
_s3_main = OrderedDict()
_s3_ghost = OrderedDict() # Key -> Origin (True=Main, False=Small)
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
    global _s3_small, _s3_main, _s3_ghost, _s3_freq
    _check_reset(cache_snapshot)

    # 10% of capacity for probation queue
    s_target = max(1, int(cache_snapshot.capacity * 0.1))
    
    while True:
        # Determine which queue to scan
        # Prefer evicting from Small if it's over capacity OR if Main is empty
        scan_small = len(_s3_small) > s_target or not _s3_main
        
        # Fallback if Small is empty (but scan_small was true implies Main is empty)
        if scan_small and not _s3_small:
            if _s3_main:
                scan_small = False # Retry Main
            else:
                # Both empty? (Should only happen if cache is empty)
                if cache_snapshot.cache:
                    return next(iter(cache_snapshot.cache))
                return None
        
        if scan_small:
            # Process Small Queue (Probation)
            cand, _ = _s3_small.popitem(last=False)
            
            # Consistency check
            if cand not in cache_snapshot.cache:
                _s3_freq.pop(cand, None)
                continue
            
            freq = _s3_freq.get(cand, 0)
            if freq > 1:
                # Promotion: Requires >1 hits (Two-Hit Promotion)
                _s3_main[cand] = None
                _s3_freq[cand] = 0
            elif freq == 1:
                # Probation: Had 1 hit, give second chance in Small
                # Reinsert to Small tail, reset freq to require new proof
                _s3_small[cand] = None
                _s3_freq[cand] = 0
            else:
                # Evict: No hits in this pass
                # Track in Ghost (Origin = Small/False)
                _s3_ghost[cand] = False
                if len(_s3_ghost) > cache_snapshot.capacity * 2:
                    _s3_ghost.popitem(last=False)
                return cand

        else: 
            # Process Main Queue (Protected)
            cand, _ = _s3_main.popitem(last=False)
            
            if cand not in cache_snapshot.cache:
                _s3_freq.pop(cand, None)
                continue
            
            freq = _s3_freq.get(cand, 0)
            if freq > 0:
                # Reinsert to Main
                _s3_main[cand] = None
                _s3_freq[cand] = 0
            else:
                # Candidate for eviction
                # Conditional Demotion: Only demote if Small has space (strictly less than target)
                if len(_s3_small) < s_target:
                    _s3_small[cand] = None
                    # Grace period: Start with freq=1 so it survives one Small pass
                    _s3_freq[cand] = 1
                else:
                    # Main is cold and Small is full -> Evict directly
                    # Track in Ghost (Origin = Main/True)
                    _s3_ghost[cand] = True
                    if len(_s3_ghost) > cache_snapshot.capacity * 2:
                        _s3_ghost.popitem(last=False)
                    return cand

def update_after_hit(cache_snapshot, obj):
    global _s3_freq
    _check_reset(cache_snapshot)
    # Cap frequency at 3
    curr = _s3_freq.get(obj.key, 0)
    _s3_freq[obj.key] = min(curr + 1, 3)

def update_after_insert(cache_snapshot, obj):
    global _s3_small, _s3_main, _s3_ghost, _s3_freq
    _check_reset(cache_snapshot)
    
    key = obj.key
    # Check Ghost
    if key in _s3_ghost:
        from_main = _s3_ghost.pop(key)
        if from_main:
            # Ghost from Main -> Main
            if key not in _s3_main:
                 _s3_main[key] = None
        else:
            # Ghost from Small -> Small
            if key not in _s3_small and key not in _s3_main:
                _s3_small[key] = None
    else:
        # New insert -> Small
        if key not in _s3_small and key not in _s3_main:
            _s3_small[key] = None
    
    # Reset freq on insert/re-insert (assumed cold until hit)
    _s3_freq[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    global _s3_freq
    _check_reset(cache_snapshot)
    # Cleanup freq. Queue removal is handled in evict.
    _s3_freq.pop(evicted_obj.key, None)
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