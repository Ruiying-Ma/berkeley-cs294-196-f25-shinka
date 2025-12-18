# EVOLVE-BLOCK-START
"""
W-TinyLFU Eviction Algorithm Implementation
Components:
1. Window Cache (1%): Admission buffer for new items.
2. Main Cache (99%): Segmented LRU (SLRU) with Probation (20%) and Protected (80%).
3. TinyLFU Admission: Frequency-based filter (Doorkeeper + Counter) to admit items from Window to Main.
"""

from collections import OrderedDict

class WTinyLFUState:
    def __init__(self, capacity):
        self.capacity = capacity
        # Cache Segments
        self.window = OrderedDict()      # Small LRU for new items
        self.probation = OrderedDict()   # SLRU Probation (A1)
        self.protected = OrderedDict()   # SLRU Protected (Am)
        
        # Frequency Counting (Approximate)
        self.freq = {}
        self.doorkeeper = set()
        self.access_counter = 0
        
        # Configuration
        # Window size: small to filter scans, but large enough to capture short term locality
        self.window_limit = max(1, int(capacity * 0.01))
        self.main_limit = capacity - self.window_limit
        self.protected_limit = int(self.main_limit * 0.8)
        
    def get_freq(self, key):
        return self.freq.get(key, 0)
    
    def record_access(self, key):
        self.access_counter += 1
        if key not in self.doorkeeper:
            self.doorkeeper.add(key)
        else:
            self.freq[key] = self.freq.get(key, 0) + 1
            
        # Aging process
        if self.access_counter >= self.capacity * 10:
            self.age_frequencies()
            
    def age_frequencies(self):
        self.access_counter = 0
        self.doorkeeper.clear()
        # Halve frequencies
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
        # Ensure Protected doesn't exceed limit
        while len(self.protected) > self.protected_limit:
            # Demote from Protected LRU to Probation MRU
            k, _ = self.protected.popitem(last=False)
            self.probation[k] = None # Insert as MRU (default for new key in dict)

_state = None

def get_state(cache_snapshot):
    global _state
    # Check if cache restarted or changed
    current_id = id(cache_snapshot.cache)
    if _state is None or getattr(_state, 'cache_id', None) != current_id:
        _state = WTinyLFUState(cache_snapshot.capacity)
        _state.cache_id = current_id
    
    # Sync check (rare but necessary if simulator diverged)
    total_len = len(_state.window) + len(_state.probation) + len(_state.protected)
    # Allow small drift during transitions (evict/insert)
    if abs(total_len - len(cache_snapshot.cache)) > 5:
        # Rebuild state from snapshot if desync detected
        _state = WTinyLFUState(cache_snapshot.capacity)
        _state.cache_id = current_id
        # Heuristic: put everything in probation to reset
        for k in cache_snapshot.cache:
            _state.probation[k] = None
    
    return _state

def evict(cache_snapshot, obj):
    '''
    Decide which object to evict.
    Policy:
    - If Window > Limit: Duel Window LRU vs Probation LRU.
    - Else: Evict Probation LRU (grow Window).
    '''
    state = get_state(cache_snapshot)
    
    # Candidates
    window_candidate = next(iter(state.window)) if state.window else None
    
    # Find Main candidate (Probation LRU, fallback to Protected LRU)
    probation_candidate = next(iter(state.probation)) if state.probation else None
    
    if probation_candidate is None and state.protected:
        # Fallback to Protected if Probation is empty
        probation_candidate = next(iter(state.protected))

    # Decision Logic
    if len(state.window) >= state.window_limit:
        # Window is full, duel!
        if window_candidate and probation_candidate:
            freq_w = state.get_freq(window_candidate)
            freq_p = state.get_freq(probation_candidate)
            
            if freq_w > freq_p:
                return probation_candidate
            else:
                return window_candidate
        elif window_candidate:
            return window_candidate
        else:
            return probation_candidate 
            
    else:
        # Window has space, prefer evicting from Main to let Window fill
        if probation_candidate:
            return probation_candidate
        elif window_candidate:
            return window_candidate
            
    # Emergency Fallback
    if cache_snapshot.cache:
        return next(iter(cache_snapshot.cache))
    return None

def update_after_hit(cache_snapshot, obj):
    '''
    Update internal state on hit.
    '''
    state = get_state(cache_snapshot)
    key = obj.key
    state.record_access(key)
    
    if key in state.window:
        # Move to MRU
        state.window.move_to_end(key)
    elif key in state.probation:
        # Promote to Protected
        del state.probation[key]
        state.protected[key] = None
    elif key in state.protected:
        # Move to MRU
        state.protected.move_to_end(key)
        
    state.maintain_slru_invariant()

def update_after_insert(cache_snapshot, obj):
    '''
    Insert new object into Window.
    '''
    state = get_state(cache_snapshot)
    key = obj.key
    state.record_access(key)
    
    # Always insert into Window
    state.window[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Remove evicted object from internal state.
    '''
    state = get_state(cache_snapshot)
    key = evicted_obj.key
    
    if key in state.window:
        del state.window[key]
    elif key in state.probation:
        del state.probation[key]
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