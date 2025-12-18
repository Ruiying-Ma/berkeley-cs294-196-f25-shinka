# EVOLVE-BLOCK-START
from collections import OrderedDict

# Global State
# S3-FIFO with Demotion (S3-FIFO-D)
# Implements a small probationary queue (S) and a main protected queue (M).
# Also tracks a ghost queue for history.
# Crucially, implements state reset to handle sequential trace execution.

small_q = OrderedDict()
main_q = OrderedDict()
ghost_q = OrderedDict()
freq_map = {}
last_trace_time = -1

def _reset_state_if_needed(snapshot):
    """
    Detects if a new trace has started by checking if access_count decreased.
    Resets all global structures if so.
    """
    global last_trace_time, small_q, main_q, ghost_q, freq_map
    current_time = snapshot.access_count
    
    # If time went backwards, it's a new trace
    if current_time < last_trace_time:
        small_q.clear()
        main_q.clear()
        ghost_q.clear()
        freq_map.clear()
    
    last_trace_time = current_time

def evict(cache_snapshot, obj):
    """
    S3-FIFO-D Eviction Policy.
    - S (Small): FIFO Probation queue. 10% of cache.
    - M (Main): FIFO Protected queue. 90% of cache.
    - Demotion: Items evicted from M with 0 frequency are demoted to S.
    """
    global small_q, main_q, ghost_q, freq_map
    _reset_state_if_needed(cache_snapshot)

    # Calculate target size for Small queue (10% of capacity)
    # Ensure at least 1 slot
    capacity = cache_snapshot.capacity
    s_capacity = max(int(capacity * 0.1), 1)

    while True:
        # Check Small Queue (S) if it's oversize or if Main is empty
        # This prioritizes cleaning up S, or filling M if needed.
        if len(small_q) > s_capacity or not main_q:
            if not small_q:
                # Both queues empty (shouldn't happen on eviction of full cache)
                # Fallback to arbitrary eviction if state desync
                if cache_snapshot.cache:
                    return next(iter(cache_snapshot.cache))
                return None
            
            candidate, _ = small_q.popitem(last=False) # Pop head (FIFO)
            
            freq = freq_map.get(candidate, 0)
            if freq > 0:
                # Promotion: S -> M
                # Item proved usefulness. Move to Main.
                # Reset frequency to require new proof in Main.
                main_q[candidate] = None
                freq_map[candidate] = 0
                continue
            else:
                # Eviction: S -> Ghost
                # No hits in probation. Evict.
                ghost_q[candidate] = None
                if candidate in freq_map:
                    del freq_map[candidate]
                
                # Manage Ghost Size
                if len(ghost_q) > capacity * 2:
                    ghost_q.popitem(last=False)
                
                return candidate

        else:
            # Check Main Queue (M)
            # Standard S3-FIFO checks M when S is within budget.
            candidate, _ = main_q.popitem(last=False) # Pop head
            
            freq = freq_map.get(candidate, 0)
            if freq > 0:
                # Re-insertion: Give second chance in M
                # Decay frequency.
                main_q[candidate] = None # Move to tail
                freq_map[candidate] = freq - 1
                continue
            else:
                # Demotion: M -> S
                # Item was cold in M. Give one last chance in Probation (S).
                # This is "Demotion" rather than direct eviction.
                small_q[candidate] = None
                freq_map[candidate] = 0
                continue

def update_after_hit(cache_snapshot, obj):
    global freq_map
    _reset_state_if_needed(cache_snapshot)
    
    # Increment frequency, cap at 3
    # Cap prevents one hot burst from protecting an item forever
    freq_map[obj.key] = min(freq_map.get(obj.key, 0) + 1, 3)

def update_after_insert(cache_snapshot, obj):
    global small_q, main_q, ghost_q, freq_map
    _reset_state_if_needed(cache_snapshot)
    
    key = obj.key
    if key in ghost_q:
        # Ghost Hit: Promote to Main
        # It was recently evicted, so it's a recall.
        main_q[key] = None
        freq_map[key] = 0
        del ghost_q[key]
    else:
        # New Insert: Start in Small (Probation)
        small_q[key] = None
        freq_map[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    global freq_map
    _reset_state_if_needed(cache_snapshot)
    
    key = evicted_obj.key
    if key in freq_map:
        del freq_map[key]
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