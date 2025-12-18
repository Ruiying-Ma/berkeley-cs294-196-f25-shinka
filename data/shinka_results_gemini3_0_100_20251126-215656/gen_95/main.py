# EVOLVE-BLOCK-START
from collections import OrderedDict

# S3-FIFO with Ghost-Gated Admission (s3fifo_ghost_gated)
#
# A robust cache eviction algorithm designed for scan resistance and loop capturing.
# Components:
# 1. Filter Queue (F): 
#    - Entry point for all NEW items (not in history).
#    - Serves as a "Gate". Items here are transient and evicted with highest priority.
#    - A hit in Filter promotes the item to Main, proving usefulness.
# 2. Small Queue (S):
#    - Probationary queue (5% of capacity).
#    - Receives items demoted from Main. Gives them a "second chance".
# 3. Main Queue (M):
#    - Protected queue (95% logical capacity).
#    - Items persist here based on frequency.
# 4. Ghost Registry (G):
#    - Tracks history of evicted items (5x capacity).
#    - Re-insertion of a Ghost item bypasses Filter and goes to Main (Loop capture).

filter_q = OrderedDict()
small_q = OrderedDict()
main_q = OrderedDict()
ghost_q = OrderedDict()
freq_map = {}
last_ts = -1

def _reset(snapshot):
    """
    Resets global state if a new trace execution is detected.
    """
    global filter_q, small_q, main_q, ghost_q, freq_map, last_ts
    if snapshot.access_count < last_ts:
        filter_q.clear()
        small_q.clear()
        main_q.clear()
        ghost_q.clear()
        freq_map.clear()
    last_ts = snapshot.access_count

def evict(cache_snapshot, obj):
    """
    Eviction Logic:
    1. Filter Queue (Transient) -> Evict immediately.
    2. Small Queue (Probation) -> Check promotion or evict.
    3. Main Queue (Protected) -> Decay or demote to Small.
    """
    global filter_q, small_q, main_q, ghost_q
    _reset(cache_snapshot)
    
    cap = cache_snapshot.capacity
    # Small queue target: 5% of capacity.
    s_target = max(int(cap * 0.05), 1)

    while True:
        # 1. Flush Filter Queue (Transient items)
        if filter_q:
            k, _ = filter_q.popitem(last=False)
            # Record in Ghost (it was seen, now gone)
            ghost_q[k] = None
            if len(ghost_q) > cap * 5:
                ghost_q.popitem(last=False)
            
            if k in freq_map: del freq_map[k]
            return k

        # 2. Process Small Queue (Probation)
        # If over budget OR Main is empty (must force movement)
        if len(small_q) > s_target or not main_q:
            if not small_q:
                # Both empty? Should not happen if cache full.
                break
            
            k, _ = small_q.popitem(last=False)
            f = freq_map.get(k, 0)
            
            if f > 0:
                # Promotion: S -> M
                # Item survived probation. Move to Main.
                main_q[k] = None
                freq_map[k] = 0 # Reset freq
                continue
            else:
                # Eviction: S -> Ghost
                ghost_q[k] = None
                if len(ghost_q) > cap * 5:
                    ghost_q.popitem(last=False)
                if k in freq_map: del freq_map[k]
                return k

        # 3. Process Main Queue (Protected)
        if main_q:
            k, _ = main_q.popitem(last=False)
            f = freq_map.get(k, 0)
            
            if f > 0:
                # Reinsert: M -> M (Decay)
                # Give second chance in Main
                main_q[k] = None
                freq_map[k] = f - 1
                continue
            else:
                # Demotion: M -> S
                # Move to Small for final probation
                small_q[k] = None
                freq_map[k] = 0
                continue
        
        break

    # Fallback
    if cache_snapshot.cache:
        return next(iter(cache_snapshot.cache))
    return None

def update_after_hit(cache_snapshot, obj):
    """
    Hit Handling:
    - Filter hits -> Promote to Main.
    - Increment Frequency (Cap 7).
    """
    global filter_q, main_q, freq_map
    _reset(cache_snapshot)
    
    k = obj.key
    # Cap frequency at 7 to distinguish very hot items
    freq_map[k] = min(freq_map.get(k, 0) + 1, 7)
    
    if k in filter_q:
        # Transient item proved useful! Rescue to Main immediately.
        # This prevents it from being flushed by the Filter eviction.
        del filter_q[k]
        main_q[k] = None

def update_after_insert(cache_snapshot, obj):
    """
    Insert Handling:
    - Ghost Hit -> Main (bypass Filter).
    - New -> Filter (Gate).
    """
    global filter_q, main_q, ghost_q, freq_map
    _reset(cache_snapshot)
    
    k = obj.key
    freq_map[k] = 0 # Initialize freq
    
    if k in ghost_q:
        # Ghost Hit: Recall to Main
        main_q[k] = None
        del ghost_q[k]
    else:
        # New: Insert to Filter (Transient Gate)
        filter_q[k] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    global freq_map
    if evicted_obj.key in freq_map:
        del freq_map[evicted_obj.key]
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