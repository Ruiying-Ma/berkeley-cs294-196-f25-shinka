# EVOLVE-BLOCK-START
"""
Optimized W-TinyLFU with Segment-Aware SLRU and Doorkeeper
Combines robust state management with size-aware eviction logic.
- 1% Window (Byte-based).
- SLRU Main Cache (Probation/Protected) with 80% Protected limit.
- Frequency Sketch with 4-bit counters (max 15) and Aging.
- Doorkeeper to filter one-hit wonders.
- Segment-Aware Duel: Adjusts eviction bias based on whether the Main victim is in Probation (easier to evict) or Protected (harder to evict).
"""
from collections import OrderedDict

class TinyLFUState:
    def __init__(self):
        self.window = OrderedDict()      # key -> size
        self.probation = OrderedDict()   # key -> size
        self.protected = OrderedDict()   # key -> size

        self.window_size = 0
        self.protected_size = 0

        self.freq = {}                   # key -> count
        self.doorkeeper = set()

        # Access counting for aging and reset detection
        self.access_count = 0
        self.last_trace_access = -1

    def check_trace_reset(self, snapshot_access_count):
        # Detect if trace restarted or new trace began reusing memory
        if snapshot_access_count < self.last_trace_access:
            self.window.clear()
            self.probation.clear()
            self.protected.clear()
            self.window_size = 0
            self.protected_size = 0
            self.freq.clear()
            self.doorkeeper.clear()
            self.access_count = 0
        self.last_trace_access = snapshot_access_count

    def get_freq(self, key):
        if key in self.freq:
            return self.freq[key]
        if key in self.doorkeeper:
            return 1
        return 0

_states = {}

def get_state(cache_snapshot):
    cid = id(cache_snapshot.cache)
    if cid not in _states:
        _states[cid] = TinyLFUState()
    return _states[cid]

def evict(cache_snapshot, obj):
    state = get_state(cache_snapshot)
    state.check_trace_reset(cache_snapshot.access_count)

    # Dynamic Parameters
    capacity = cache_snapshot.capacity
    w_cap = max(1, int(capacity * 0.01))

    # Candidates
    cand_w = next(iter(state.window)) if state.window else None

    cand_m = None
    is_probation = False
    if state.probation:
        cand_m = next(iter(state.probation))
        is_probation = True
    elif state.protected:
        cand_m = next(iter(state.protected))

    # Handle empty cases
    if not cand_w: return cand_m
    if not cand_m: return cand_w

    fw = state.get_freq(cand_w)
    fm = state.get_freq(cand_m)

    # 1. Window Growth Phase
    # Prefer evicting Main to grow Window, unless Main is strictly better
    if state.window_size < w_cap:
        if fm > fw:
            return cand_w
        return cand_m

    # 2. Steady State: Segment-aware Duel
    if is_probation:
        # In Probation, if Window item is at least as good, evict Probation item.
        # This facilitates promotion of new good items.
        if fw >= fm:
            return cand_m
        else:
            return cand_w
    else:
        # In Protected, Main item is an incumbent heavy hitter.
        # Require Window item to be significantly better.
        if fw > fm + 1:
            return cand_m
        else:
            return cand_w

def update_after_hit(cache_snapshot, obj):
    state = get_state(cache_snapshot)
    state.check_trace_reset(cache_snapshot.access_count)
    key = obj.key
    state.access_count += 1

    capacity = cache_snapshot.capacity

    # Frequency Update
    if key in state.freq:
        state.freq[key] = min(state.freq[key] + 1, 15)
    elif key in state.doorkeeper:
        state.doorkeeper.remove(key)
        state.freq[key] = 2
    else:
        state.doorkeeper.add(key)

    # Doorkeeper Reset
    if len(state.doorkeeper) > capacity * 2:
        state.doorkeeper.clear()

    # Aging
    if state.access_count >= capacity * 5:
        rem = []
        for k in state.freq:
            state.freq[k] //= 2
            if state.freq[k] == 0: rem.append(k)
        for k in rem: del state.freq[k]
        state.access_count = 0

    # SLRU Management
    if key in state.window:
        state.window.move_to_end(key)
    elif key in state.protected:
        state.protected.move_to_end(key)
    elif key in state.probation:
        # Promote to Protected
        size = state.probation.pop(key)
        state.protected[key] = size
        state.protected_size += size

        # Enforce Protected Limit
        p_limit = int(capacity * 0.8)
        while state.protected_size > p_limit and state.protected:
            k, s = state.protected.popitem(last=False)
            state.protected_size -= s
            state.probation[k] = s
            state.probation.move_to_end(k)

def update_after_insert(cache_snapshot, obj):
    state = get_state(cache_snapshot)
    state.check_trace_reset(cache_snapshot.access_count)
    key = obj.key
    size = obj.size
    state.access_count += 1

    capacity = cache_snapshot.capacity

    # Frequency Update
    if key in state.freq:
        state.freq[key] = min(state.freq[key] + 1, 15)
    elif key in state.doorkeeper:
        state.doorkeeper.remove(key)
        state.freq[key] = 2
    else:
        state.doorkeeper.add(key)

    # Doorkeeper Reset
    if len(state.doorkeeper) > capacity * 2:
        state.doorkeeper.clear()

    # Aging
    if state.access_count >= capacity * 5:
        rem = []
        for k in state.freq:
            state.freq[k] //= 2
            if state.freq[k] == 0: rem.append(k)
        for k in rem: del state.freq[k]
        state.access_count = 0

    # Insert into Window
    state.window[key] = size
    state.window_size += size

    # Window Overflow
    w_cap = max(1, int(capacity * 0.01))
    while state.window_size > w_cap and state.window:
        k, s = state.window.popitem(last=False)
        state.window_size -= s
        state.probation[k] = s
        state.probation.move_to_end(k)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    state = get_state(cache_snapshot)
    key = evicted_obj.key
    size = evicted_obj.size

    if key in state.window:
        del state.window[key]
        state.window_size -= size
    elif key in state.probation:
        del state.probation[key]
    elif key in state.protected:
        val = state.protected.pop(key)
        state.protected_size -= val
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