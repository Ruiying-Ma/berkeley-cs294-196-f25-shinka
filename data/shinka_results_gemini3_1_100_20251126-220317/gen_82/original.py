# EVOLVE-BLOCK-START
"""
W-TinyLFU with Enhanced Incumbency Bias and Demotion Protection
Improvements:
1. Strong Incumbency Bias: Window victims must beat Main victims by a margin (freq + 1).
2. Demotion Protection: Items demoted from Protected to Probation get a 'second chance' bonus in duels.
3. Window Promotion: Hits in Window promote directly to Main (Probation), fast-tracking successful items.
4. Adaptive Doorkeeper & Frequency: Tuned for responsiveness and history retention.
"""

from collections import OrderedDict

class WTinyLFUState:
    def __init__(self, capacity):
        self.capacity = capacity
        # 1% Window, but at least 1 slot
        self.window_limit = max(1, int(capacity * 0.01))
        self.main_limit = capacity - self.window_limit
        # SLRU split: 80% Protected
        self.protected_limit = int(self.main_limit * 0.8)
        
        # Segments
        self.window = OrderedDict()
        self.probation = OrderedDict()
        self.protected = OrderedDict()
        self.demoted = set() # Set of keys demoted from Protected
        
        # Frequency Sketch
        self.freq = {}
        self.doorkeeper = set()
        self.access_counter = 0
        
        # Parameters
        self.max_freq = 15
        self.aging_interval = capacity * 10
        self.doorkeeper_limit = capacity * 2
        
    def get_freq(self, key):
        val = self.freq.get(key, 0)
        if key in self.doorkeeper:
            val += 1
        return val
    
    def record_access(self, key):
        self.access_counter += 1
        
        # Doorkeeper / Frequency logic
        if key not in self.doorkeeper:
            self.doorkeeper.add(key)
        else:
            curr = self.freq.get(key, 0)
            if curr < self.max_freq:
                self.freq[key] = curr + 1
            
        # Aging
        if self.access_counter >= self.aging_interval:
            self.age_frequencies()
            self.access_counter = 0
            
        # Doorkeeper Reset
        if len(self.doorkeeper) > self.doorkeeper_limit:
            self.doorkeeper.clear()
            
    def age_frequencies(self):
        # Halving
        removals = []
        for k, v in self.freq.items():
            new_v = v // 2
            if new_v == 0:
                removals.append(k)
            else:
                self.freq[k] = new_v
        for k in removals:
            del self.freq[k]
            
    def maintain_slru_invariant(self):
        while len(self.protected) > self.protected_limit:
            k, _ = self.protected.popitem(last=False)
            self.probation[k] = None
            self.demoted.add(k)

_state = None

def get_state(cache_snapshot):
    global _state
    current_id = id(cache_snapshot.cache)
    if _state is None or getattr(_state, 'cache_id', None) != current_id:
        _state = WTinyLFUState(cache_snapshot.capacity)
        _state.cache_id = current_id
    
    # Sync Check (Lazy rebuild if drift is too high)
    total_len = len(_state.window) + len(_state.probation) + len(_state.protected)
    if abs(total_len - len(cache_snapshot.cache)) > 5:
        # Rebuild state from scratch to be safe
        _state = WTinyLFUState(cache_snapshot.capacity)
        _state.cache_id = current_id
        # Assign all existing to probation as a fallback
        for k in cache_snapshot.cache:
            _state.probation[k] = None
            
    return _state

def evict(cache_snapshot, obj):
    state = get_state(cache_snapshot)
    
    # Identify candidates
    candidate_w = next(iter(state.window)) if state.window else None
    candidate_p = next(iter(state.probation)) if state.probation else None
    
    # If probation is empty, borrow from protected (should differ to maintain invariant, but need a victim)
    if not candidate_p and state.protected:
        candidate_p = next(iter(state.protected))

    # 1. Protect Window growth if it's small (evict Main to make room)
    if len(state.window) < state.window_limit:
        if candidate_p:
            return candidate_p
        return candidate_w or next(iter(cache_snapshot.cache))
    
    # 2. Duel: Window LRU vs Main (Probation) LRU
    if candidate_w and candidate_p:
        freq_w = state.get_freq(candidate_w)
        freq_p = state.get_freq(candidate_p)
        
        # Calculate Bias
        # Standard bias: 1 (Incumbency)
        # Demotion bias: +4 (If victim was recently protected)
        bias = 1
        if candidate_p in state.demoted:
            bias = 5
            
        # Window must strictly beat Main + bias to displace it
        if freq_w > freq_p + bias:
            return candidate_p
        else:
            return candidate_w
            
    # Fallbacks
    if candidate_w: return candidate_w
    if candidate_p: return candidate_p
    return next(iter(cache_snapshot.cache))

def update_after_hit(cache_snapshot, obj):
    state = get_state(cache_snapshot)
    key = obj.key
    state.record_access(key)
    
    if key in state.window:
        # Hit in Window -> Promote to Probation (Main)
        del state.window[key]
        state.probation[key] = None
    elif key in state.probation:
        # Hit in Probation -> Promote to Protected
        del state.probation[key]
        state.protected[key] = None
        if key in state.demoted:
            state.demoted.remove(key)
        state.maintain_slru_invariant()
    elif key in state.protected:
        state.protected.move_to_end(key)

def update_after_insert(cache_snapshot, obj):
    state = get_state(cache_snapshot)
    key = obj.key
    state.record_access(key)
    
    # New items go to Window
    state.window[key] = None
    
    # If Window full, migrate LRU to Probation
    # This happens if we just evicted from Probation (making space), 
    # so now Window has one extra item. We move the oldest Window item to Probation.
    if len(state.window) > state.window_limit:
        k, _ = state.window.popitem(last=False)
        state.probation[k] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    state = get_state(cache_snapshot)
    key = evicted_obj.key
    
    # Clean up state
    if key in state.window:
        del state.window[key]
    elif key in state.probation:
        del state.probation[key]
        if key in state.demoted:
            state.demoted.remove(key)
    elif key in state.protected:
        del state.protected[key]
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