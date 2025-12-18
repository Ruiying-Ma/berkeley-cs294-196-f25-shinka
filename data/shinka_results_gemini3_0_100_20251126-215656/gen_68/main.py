# EVOLVE-BLOCK-START
from collections import OrderedDict

# PRO-S3-FIFO (Probation-Rejection Optimized S3-FIFO)
# Advanced cache eviction algorithm improving on S3-FIFO with:
# - Capacity-Gated Demotion (Prevents pollution of Small queue during scans)
# - Extended Ghost History (Better loop detection)
# - Tiered Frequency & Strict Probation (Better distinction of item utility)

# Global State
q_small = OrderedDict()      # Small FIFO queue (Probation)
q_main = OrderedDict()       # Main FIFO queue (Protected)
q_ghost = OrderedDict()      # Ghost FIFO queue (History)
meta_freq = {}               # Frequency tracking
last_ts = -1                 # Timestamp for trace reset detection

def _reset_if_needed(snapshot):
    """Resets internal state if a new trace is detected based on timestamp rollback."""
    global q_small, q_main, q_ghost, meta_freq, last_ts
    if snapshot.access_count < last_ts:
        q_small.clear()
        q_main.clear()
        q_ghost.clear()
        meta_freq.clear()
    last_ts = snapshot.access_count

def evict(cache_snapshot, obj):
    '''
    Selects a victim using PRO-S3-FIFO logic.
    '''
    global q_small, q_main, q_ghost, meta_freq
    _reset_if_needed(cache_snapshot)

    capacity = cache_snapshot.capacity
    # Small queue target size (10% of cache)
    s_target = max(1, int(capacity * 0.1))
    
    # Safety loop limiter
    ops = 0
    limit = len(cache_snapshot.cache) * 2

    while ops < limit:
        ops += 1
        
        # 1. Check Small Queue (Probation)
        # Process Small if it's over budget or if Main is empty
        if len(q_small) > s_target or not q_main:
            if not q_small:
                # Fallback: If Small is empty but we are here, Main must be empty too
                # or s_target is very large. Check Main just in case.
                if q_main:
                    cand, _ = q_main.popitem(last=False)
                    q_ghost[cand] = None
                    # Cap Ghost at 4x capacity
                    if len(q_ghost) > capacity * 4:
                        q_ghost.popitem(last=False)
                    meta_freq.pop(cand, None)
                    return cand
                break # Both queues empty, use absolute fallback

            # Pop from Head of Small (FIFO/LRU)
            candidate, _ = q_small.popitem(last=False)
            
            # Check for Promotion
            freq = meta_freq.get(candidate, 0)
            if freq > 0:
                # Promote to Main
                q_main[candidate] = None
                meta_freq[candidate] = 1 # Tiered Promotion Bonus (Buffer)
                continue
            else:
                # Evict from Small -> Ghost
                q_ghost[candidate] = None
                if len(q_ghost) > capacity * 4: # Extended Ghost History
                    q_ghost.popitem(last=False)
                meta_freq.pop(candidate, None)
                return candidate

        # 2. Check Main Queue (Protected)
        else:
            # Pop from Head of Main (LRU)
            candidate, _ = q_main.popitem(last=False)
            
            # Check for Retention
            freq = meta_freq.get(candidate, 0)
            if freq > 0:
                # Reinsert into Main with Decay (Second Chance)
                q_main[candidate] = None # Move to Tail
                meta_freq[candidate] = freq - 1
                continue
            else:
                # Demotion Logic: Capacity-Gated
                # Only demote to Small if Small is NOT under pressure (has space)
                if len(q_small) < s_target:
                    q_small[candidate] = None
                    # Strict Probation: Insert at Head (LRU) so it is the next victim
                    # unless accessed immediately.
                    q_small.move_to_end(candidate, last=False)
                    meta_freq[candidate] = 0
                    continue
                else:
                    # S is full/pressured: Evict Main item directly to Ghost
                    # This prevents decaying Main items from clogging Small during scans
                    q_ghost[candidate] = None
                    if len(q_ghost) > capacity * 4:
                        q_ghost.popitem(last=False)
                    meta_freq.pop(candidate, None)
                    return candidate

    # Absolute Fallback if queues are desynchronized or empty
    if cache_snapshot.cache:
        return next(iter(cache_snapshot.cache))
    return None

def update_after_hit(cache_snapshot, obj):
    '''Increment frequency, cap at 3.'''
    global meta_freq
    _reset_if_needed(cache_snapshot)
    
    curr = meta_freq.get(obj.key, 0)
    meta_freq[obj.key] = min(curr + 1, 3)

def update_after_insert(cache_snapshot, obj):
    '''Handle new object insertion with Ghost awareness.'''
    global q_small, q_main, q_ghost, meta_freq
    _reset_if_needed(cache_snapshot)
    
    key = obj.key
    if key in q_ghost:
        # Ghost Hit: Restore to Main with Bonus
        q_main[key] = None
        meta_freq[key] = 2 # Stronger start for ghost hits
        del q_ghost[key]
    else:
        # New Item: Insert to Small (Probation)
        q_small[key] = None
        meta_freq[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''Cleanup state for evicted object.'''
    global q_small, q_main, meta_freq
    _reset_if_needed(cache_snapshot)
    
    key = evicted_obj.key
    # Ensure removal from all active queues
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