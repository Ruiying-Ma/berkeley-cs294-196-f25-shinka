# EVOLVE-BLOCK-START
"""S3-FIFO with Probabilistic Admission and Tenancy Bonus"""

# Global State
# _q_small: Probationary FIFO queue (Small)
# _q_main: Protected FIFO queue (Main)
# _q_ghost: Ghost FIFO queue (History)
# _q_ed: Early Discharge queue (Scan Junk)
# _freq: Frequency counter for objects
# _insert_cnt: Counter to drive probabilistic logic
# _last_ts: Timestamp to detect new traces
_q_small = {}
_q_main = {}
_q_ghost = {}
_q_ed = {}
_freq = {}
_insert_cnt = 0
_last_ts = -1

def _reset_if_needed(snapshot):
    global _q_small, _q_main, _q_ghost, _q_ed, _freq, _insert_cnt, _last_ts
    if snapshot.access_count < _last_ts:
        _q_small.clear()
        _q_main.clear()
        _q_ghost.clear()
        _q_ed.clear()
        _freq.clear()
        _insert_cnt = 0
    _last_ts = snapshot.access_count

def evict(cache_snapshot, obj):
    '''
    S3-FIFO eviction with Early Discharge (Scan Guard) and Tenancy Bonus.
    Prioritizes evicting from Early Discharge queue to protect Probation and Main queues.
    '''
    global _q_small, _q_main, _q_ed, _freq
    _reset_if_needed(cache_snapshot)
    
    # Target size for small queue (10% of capacity)
    curr_size = len(cache_snapshot.cache)
    s_target = max(1, int(curr_size * 0.1))
    
    while True:
        # 1. Early Discharge (Scan Guard)
        # These are items that failed admission filter. Evict them first.
        # This acts as a shield for the _q_small queue.
        if _q_ed:
            cand = next(iter(_q_ed))
            
            # Consistency check
            if cand not in cache_snapshot.cache:
                _q_ed.pop(cand, None)
                _freq.pop(cand, None)
                continue
            
            # Rescue: If an ED item got a hit, it proved it's not junk.
            # Rescue to Small queue.
            if _freq.get(cand, 0) > 0:
                _q_ed.pop(cand)
                _q_small[cand] = None
                # Keep frequency to help it promote later if hit again
                continue
            else:
                # Evict immediately
                return cand

        # 2. Small Queue (Probation)
        # Check if Small is overflowing or if Main is empty
        if len(_q_small) > s_target or not _q_main:
            if not _q_small:
                # If Small is empty, we must look at Main (handled below)
                # But if Main is also empty (should rarely happen), force fallback
                if not _q_main:
                    if cache_snapshot.cache:
                        return next(iter(cache_snapshot.cache))
                    return None
            else:
                cand = next(iter(_q_small))
                
                if cand not in cache_snapshot.cache:
                    _q_small.pop(cand, None)
                    _freq.pop(cand, None)
                    continue

                f = _freq.get(cand, 0)
                # Standard S3 promotion: freq > 0 (at least one hit in probation)
                if f > 0:
                    _q_small.pop(cand)
                    _q_main[cand] = None
                    _freq[cand] = 0 # Reset freq on promotion
                    continue
                else:
                    # Evict
                    return cand

        # 3. Main Queue (Protected)
        if _q_main:
            cand = next(iter(_q_main))
            
            if cand not in cache_snapshot.cache:
                _q_main.pop(cand, None)
                _freq.pop(cand, None)
                continue
            
            f = _freq.get(cand, 0)
            if f > 0:
                # Reinsert with Decay
                _q_main.pop(cand)
                _q_main[cand] = None # Move to MRU
                _freq[cand] = f - 1  # Decay frequency
                continue
            else:
                # Demote to Small
                # Give it one last chance in probation before eviction
                _q_main.pop(cand)
                _q_small[cand] = None
                _freq[cand] = 0
                continue
        
        # Fallback
        if cache_snapshot.cache:
             return next(iter(cache_snapshot.cache))
        return None

def update_after_hit(cache_snapshot, obj):
    global _freq
    _reset_if_needed(cache_snapshot)
    # Increment frequency, cap at 7 (High Ceiling)
    # This allows popular items to survive multiple decay cycles in Main
    _freq[obj.key] = min(_freq.get(obj.key, 0) + 1, 7)

def update_after_insert(cache_snapshot, obj):
    global _q_small, _q_main, _q_ghost, _q_ed, _freq, _insert_cnt
    _reset_if_needed(cache_snapshot)
    
    key = obj.key
    _insert_cnt += 1
    
    # 1. Ghost Promotion with Tenancy Bonus
    if key in _q_ghost:
        # If in ghost, it's a known recurring item. Promote to Main.
        if key not in _q_main and key not in _q_small and key not in _q_ed:
            _q_main[key] = None
            # Tenancy Bonus: Start with freq=2 to survive 2 decay cycles
            _freq[key] = 2 
        _q_ghost.pop(key)
        return

    # 2. New Item Admission
    if key not in _q_small and key not in _q_main and key not in _q_ed:
        curr_small_size = len(_q_small)
        # Use cache len as proxy for capacity usage
        target = max(1, int(len(cache_snapshot.cache) * 0.1))
        
        # Probabilistic Admission (Scan Guard)
        # If Small queue is full, apply filter to prevent scan pollution
        if curr_small_size >= target:
            # 25% admission to Probation, 75% to Early Discharge
            if _insert_cnt % 4 == 0:
                _q_small[key] = None
            else:
                _q_ed[key] = None # Early Discharge queue
        else:
            # If space available, admit to Probation
            _q_small[key] = None
        
        # Initialize freq
        _freq[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    global _q_small, _q_main, _q_ghost, _q_ed, _freq
    _reset_if_needed(cache_snapshot)
    
    key = evicted_obj.key
    
    # Remove from whichever queue it was in
    if key in _q_ed:
        _q_ed.pop(key)
        _q_ghost[key] = None
    elif key in _q_small:
        _q_small.pop(key)
        _q_ghost[key] = None
    elif key in _q_main:
        _q_main.pop(key)
        _q_ghost[key] = None
        
    _freq.pop(key, None)
    
    # Extended Ghost Capacity (5x) to catch larger loops
    if len(_q_ghost) > cache_snapshot.capacity * 5:
        _q_ghost.pop(next(iter(_q_ghost)), None)
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