# EVOLVE-BLOCK-START
"""
Hyper S3-FIFO (HyperS3FIFO)
Advanced implementation of S3-FIFO with:
- Expanded Ghost Registry (4x capacity) for long-loop detection.
- Enlarged Probationary Queue (20% capacity) to reduce thrashing.
- Tiered Frequency Decay: Hits in Main increment frequency (up to 3), eviction decrements.
- Capacity-Aware Demotion: M-victims only demoted to S if S has space, otherwise ghosted.
- Robust Trace Reset: Detects new traces via access count.
"""

from collections import OrderedDict

# Global State
s_queue = OrderedDict()       # Small/Probationary Queue
m_queue = OrderedDict()       # Main/Protected Queue
ghost_registry = OrderedDict() # History of evicted keys
freq_map = {}                 # Frequency counter (0-3)

def evict(cache_snapshot, obj):
    '''
    Selects a victim using Hyper S3-FIFO policy.
    Prioritizes S eviction if over budget.
    Uses frequency counters with decay in M.
    Strictly handles M->S demotion based on S capacity.
    '''
    capacity = cache_snapshot.capacity
    # Target size for the small queue (20% of capacity) to absorb larger scans
    s_target = max(int(capacity * 0.2), 1)
    
    # Extended Ghost limit (4x capacity) to catch larger loops (Trace 14/Loops)
    ghost_limit = capacity * 4

    # Safety check
    if not s_queue and not m_queue:
        return next(iter(cache_snapshot.cache))

    while True:
        # Determine which queue to operate on
        # Clean S if it's too big OR if M is empty (must provide victim)
        if len(s_queue) > s_target or len(m_queue) == 0:
            if s_queue:
                candidate, _ = s_queue.popitem(last=False) # FIFO head
                cnt = freq_map.get(candidate, 0)
                
                if cnt > 0:
                    # Accessed in S -> Promote to M
                    m_queue[candidate] = None
                    freq_map[candidate] = 0 # Reset freq on promotion
                else:
                    # Victim found in S
                    ghost_registry[candidate] = None
                    if len(ghost_registry) > ghost_limit:
                        ghost_registry.popitem(last=False)
                    return candidate
            else:
                # Should not be reached if cache is full
                # Fallback to M if S is empty (loop logic handles this naturally)
                pass

        # Operate on M
        if m_queue:
            candidate, _ = m_queue.popitem(last=False) # FIFO head
            cnt = freq_map.get(candidate, 0)
            
            if cnt > 0:
                # Accessed in M -> Reinsert at tail
                m_queue[candidate] = None
                # Decay frequency: 3->2, 2->1, 1->0
                freq_map[candidate] = cnt - 1
            else:
                # Cold in M (freq=0)
                # Conditional Demotion: Only demote to S if S is under capacity
                if len(s_queue) < s_target:
                    s_queue[candidate] = None
                    # freq remains 0
                else:
                    # S is full. Strict eviction.
                    # Add to Ghost to catch if this working set returns
                    ghost_registry[candidate] = None
                    if len(ghost_registry) > ghost_limit:
                        ghost_registry.popitem(last=False)
                    return candidate

def update_after_hit(cache_snapshot, obj):
    '''Increment frequency, cap at 3.'''
    curr = freq_map.get(obj.key, 0)
    freq_map[obj.key] = min(curr + 1, 3)

def update_after_insert(cache_snapshot, obj):
    '''Handle insertion and trace reset.'''
    # Detect new trace start using access_count
    # First access is 1. If we see <=1, it's likely a fresh run.
    if cache_snapshot.access_count <= 1:
        s_queue.clear()
        m_queue.clear()
        ghost_registry.clear()
        freq_map.clear()
    
    key = obj.key
    freq_map[key] = 0 # Initialize frequency
    
    if key in ghost_registry:
        # Ghost Hit -> Promote to Main (skip probation)
        m_queue[key] = None
        del ghost_registry[key]
    else:
        # New Item -> Probation (Small Queue)
        s_queue[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''Cleanup metadata for the evicted object.'''
    key = evicted_obj.key
    if key in freq_map:
        del freq_map[key]
    # Ensure removed from queues (usually done in evict, but safe to check)
    if key in s_queue:
        del s_queue[key]
    if key in m_queue:
        del m_queue[key]
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