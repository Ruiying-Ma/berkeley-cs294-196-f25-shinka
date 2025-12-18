# EVOLVE-BLOCK-START
"""
Smart S3-FIFO (SmartS3FIFO)
Enhancements:
- Conditional Demotion: M->S demotion only when S is under capacity.
- Strict Eviction: Direct M eviction when S is full.
- Ghost Registry: Tracks S evictions to rescue false negatives.
- Frequency Map: Uses integer counters instead of bits for future extensibility.
"""

from collections import OrderedDict

# Global State
s_queue = OrderedDict()       # Small/Probationary Queue
m_queue = OrderedDict()       # Main/Protected Queue
ghost_registry = OrderedDict() # History of S evictions
freq_map = {}                 # Frequency counter (0-3)

def evict(cache_snapshot, obj):
    '''
    Selects a victim.
    Policy:
    - If S is above target size (10%) or M is empty: Clean S.
      - S-Victim freq > 1? -> Promote to M.
      - Else -> Evict & Ghost (store freq).
    - Else (S is small enough, M has data): Clean M.
      - M-Victim accessed? -> Reinsert M.
      - Else -> Demote to S (if room) or Evict.
    '''
    # 10% capacity target for S
    capacity = cache_snapshot.capacity
    s_target = max(int(capacity * 0.1), 1)

    # Cap ghost to 4x capacity for better history
    ghost_limit = capacity * 4

    while True:
        force_s = len(s_queue) > s_target or len(m_queue) == 0

        if force_s:
            if not s_queue:
                if m_queue:
                    force_s = False
                else:
                    return next(iter(cache_snapshot.cache))

            if force_s:
                candidate, _ = s_queue.popitem(last=False)
                cnt = freq_map.get(candidate, 0)

                # Strict promotion: require > 1 hit (at least 2 accesses)
                if cnt > 1:
                    m_queue[candidate] = None
                    freq_map[candidate] = 0 # Reset frequency on move
                else:
                    # Evict and store frequency in ghost
                    ghost_registry[candidate] = cnt
                    if len(ghost_registry) > ghost_limit:
                        ghost_registry.popitem(last=False)
                    return candidate

        if not force_s:
            if not m_queue:
                continue

            candidate, _ = m_queue.popitem(last=False)
            cnt = freq_map.get(candidate, 0)

            if cnt > 0:
                # Accessed in M -> Reinsert M
                m_queue[candidate] = None
                freq_map[candidate] = 0
            else:
                # Cold in M
                if len(s_queue) < s_target:
                    s_queue[candidate] = None
                else:
                    return candidate

def update_after_hit(cache_snapshot, obj):
    '''Increment frequency, cap at 7.'''
    curr = freq_map.get(obj.key, 0)
    freq_map[obj.key] = min(curr + 1, 7)

def update_after_insert(cache_snapshot, obj):
    '''Insert based on Ghost history and frequency.'''
    key = obj.key

    if key in ghost_registry:
        # Ghost hit - Restore frequency and increment
        past_freq = ghost_registry[key]
        del ghost_registry[key]

        # Increment to count this access
        new_freq = past_freq + 1
        freq_map[key] = min(new_freq, 7)

        if new_freq > 1:
            # Promote to Main if accessed > 1 time total
            m_queue[key] = None
            freq_map[key] = 0 # Reset for M lifecycle
        else:
            # Not enough hits yet, put in S with updated freq
            s_queue[key] = None
    else:
        # New -> Insert to S
        s_queue[key] = None
        freq_map[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''Cleanup metadata.'''
    key = evicted_obj.key
    if key in freq_map:
        del freq_map[key]
    # Queues are handled in evict, but safety check
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