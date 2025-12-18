# EVOLVE-BLOCK-START
from collections import deque

# Global Data Structures
# q_s: Small/Probationary FIFO Queue
# q_m: Main/Protected FIFO Queue
# ghost: Set of keys recently evicted from S (Ghost Registry)
# ghost_fifo: FIFO order for ghost keys to manage capacity
# access_bits: Set of keys that have been accessed (reference bit)
q_s = deque()
q_m = deque()
ghost = set()
ghost_fifo = deque()
access_bits = set()

# Adaptive Parameters
# s_ratio: Target fraction of capacity for the Small queue (0.01 to 0.9)
s_ratio = 0.1
# last_ts: Timestamp of last access to detect trace changes
last_ts = -1

def _reset_globals(cache_snapshot):
    """
    Resets global state if a new trace is detected.
    """
    global q_s, q_m, ghost, ghost_fifo, access_bits, s_ratio, last_ts
    ts = cache_snapshot.access_count
    
    # Reset if time moved backward (new trace) or cache is effectively starting
    if ts < last_ts or ts == 0:
        q_s.clear()
        q_m.clear()
        ghost.clear()
        ghost_fifo.clear()
        access_bits.clear()
        s_ratio = 0.1
        
    last_ts = ts

def evict(cache_snapshot, obj):
    """
    Selects a victim using Adaptive S3-FIFO with Demotion.
    """
    _reset_globals(cache_snapshot)
    global s_ratio
    
    capacity = cache_snapshot.capacity
    target_s = max(1, int(capacity * s_ratio))
    
    while True:
        # Determine eviction source:
        # Evict from S if it exceeds target size OR if M is empty
        evict_from_s = (len(q_s) > target_s) or (len(q_m) == 0)
        
        if evict_from_s:
            if not q_s:
                # Should not happen if cache is not empty
                return next(iter(cache_snapshot.cache))
            
            victim = q_s.popleft()
            
            if victim in access_bits:
                # Second Chance: Promote to M
                access_bits.discard(victim)
                q_m.append(victim)
            else:
                # Victim Found in S
                # Record in Ghost Registry
                if victim not in ghost:
                    ghost.add(victim)
                    ghost_fifo.append(victim)
                    # Limit ghost size to 2x capacity for extended history
                    if len(ghost) > capacity * 2:
                        rem = ghost_fifo.popleft()
                        if rem in ghost:
                            ghost.remove(rem)
                return victim
        else:
            # Check M Queue
            if not q_m:
                # Fallback
                if q_s: continue
                return next(iter(cache_snapshot.cache))
            
            victim = q_m.popleft()
            
            if victim in access_bits:
                # Second Chance: Reinsert into M
                access_bits.discard(victim)
                q_m.append(victim)
            else:
                # M Victim -> Demote to S Tail
                # This gives the item a second chance in the probationary queue
                # It will compete with new insertions
                q_s.append(victim)
                # Loop continues; this may trigger S eviction in next iteration

def update_after_hit(cache_snapshot, obj):
    """
    Mark object as accessed.
    """
    _reset_globals(cache_snapshot)
    access_bits.add(obj.key)

def update_after_insert(cache_snapshot, obj):
    """
    Handle new insertions and adapt S-queue size.
    """
    _reset_globals(cache_snapshot)
    global s_ratio
    key = obj.key
    
    if key in ghost:
        # Ghost Hit: Item was evicted but needed.
        # 1. Promote directly to M (Rescue)
        q_m.append(key)
        access_bits.add(key)
        ghost.remove(key)
        
        # 2. Adapt: Increase S size. 
        # Ghost hits imply S was too small to hold the working set or loop.
        s_ratio = min(0.9, s_ratio + 0.02)
    else:
        # Standard Insert: New item goes to S
        q_s.append(key)
        access_bits.discard(key)
        
        # Adapt: Slowly decay S size.
        # If no ghost hits occur, we assume M is doing a good job and S is just for filtering.
        # Shrink S to give M more space.
        s_ratio = max(0.01, s_ratio - 0.0002)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    """
    Cleanup access bits.
    """
    _reset_globals(cache_snapshot)
    access_bits.discard(evicted_obj.key)
    # Ghost cleanup is handled in evict/insert
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