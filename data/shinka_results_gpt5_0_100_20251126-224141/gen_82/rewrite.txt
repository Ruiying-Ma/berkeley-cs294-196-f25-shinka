# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

from collections import OrderedDict

# External timestamp map kept for compatibility and tie-breaking
m_key_timestamp = dict()


class ARCManager:
    def __init__(self):
        # ARC metadata
        self.T1 = OrderedDict()  # recent, resident
        self.T2 = OrderedDict()  # frequent, resident
        self.B1 = OrderedDict()  # ghost of T1
        self.B2 = OrderedDict()  # ghost of T2
        self.p = 0               # target size of T1
        self.C = None            # capacity (in objects)
        # Adaptation and scan handling
        self.last_ghost_hit_access = -1
        self.cold_streak = 0
        self.scan_guard_until = -1
        self.bias_armed = False        # for one-shot demotion bias during scan guard
        self.last_replaced_from = None # 'T1' or 'T2'

    # -------------- Utilities --------------

    def _ensure_capacity(self, cache_snapshot):
        if self.C is None:
            self.C = max(int(cache_snapshot.capacity), 1)
        self.p = max(0, min(self.p, self.C))

    def _move_to_mru(self, od, k):
        if k in od:
            od.pop(k, None)
        od[k] = True

    def _pop_lru(self, od):
        if od:
            k, _ = od.popitem(last=False)
            return k
        return None

    def _trim_ghosts(self):
        # Keep |B1| + |B2| <= C with hysteresis toward targets (p and C-p)
        if self.C is None:
            return
        total = len(self.B1) + len(self.B2)
        h = max(1, self.C // 32)  # hysteresis slack
        while total > self.C:
            tgt_B1 = min(self.C, max(0, self.p))
            tgt_B2 = max(0, self.C - tgt_B1)
            # Prefer trimming side exceeding target + h
            if len(self.B1) > tgt_B1 + h and self.B1:
                self._pop_lru(self.B1)
            elif len(self.B2) > tgt_B2 + h and self.B2:
                self._pop_lru(self.B2)
            else:
                # If both within slack, trim the larger side
                if len(self.B1) >= len(self.B2) and self.B1:
                    self._pop_lru(self.B1)
                elif self.B2:
                    self._pop_lru(self.B2)
                else:
                    break
            total = len(self.B1) + len(self.B2)

    def _resync(self, cache_snapshot):
        # Keep residents aligned with actual cache content; ghosts disjoint
        cache_keys = set(cache_snapshot.cache.keys())
        for k in list(self.T1.keys()):
            if k not in cache_keys:
                self.T1.pop(k, None)
        for k in list(self.T2.keys()):
            if k not in cache_keys:
                self.T2.pop(k, None)
        # Any cached but untracked -> assume recent (T1)
        for k in cache_keys:
            if k not in self.T1 and k not in self.T2:
                self.T1[k] = True
        # Ghosts must be disjoint from residents
        for k in list(self.B1.keys()):
            if k in cache_keys or k in self.T1 or k in self.T2:
                self.B1.pop(k, None)
        for k in list(self.B2.keys()):
            if k in cache_keys or k in self.T1 or k in self.T2:
                self.B2.pop(k, None)
        self._trim_ghosts()

    def _idle_decay_p(self, cache_snapshot):
        # Gentle decay: if a long idle since last ghost hit, slowly reduce p toward 0
        if self.last_ghost_hit_access >= 0 and self.C:
            idle = cache_snapshot.access_count - self.last_ghost_hit_access
            if idle > self.C and self.p > 0:
                self.p = max(0, self.p - 1)

    # -------------- Core policy --------------

    def _effective_p(self, cache_snapshot):
        # Compute effective p with scan guard and one-shot demotion bias when applicable
        eff_p = self.p
        now = cache_snapshot.access_count
        if now <= self.scan_guard_until:
            window = min(8, max(1, self.C // 16))
            # Gentle drop scaled by cold streak beyond half capacity
            step_unit = max(1, self.C // 16)
            extra = max(0, (self.cold_streak - max(1, self.C // 2)) // step_unit)
            drop = min(step_unit, 1 + extra)
            eff_p = max(0, eff_p - drop)
            # Time-bounded demotion bias (one-shot) if frequency hints absent
            if self.bias_armed and len(self.B2) == 0 and len(self.T2) > len(self.T1):
                eff_p = 0
                # consume the bias for this REPLACE
                self.bias_armed = False
        return eff_p

    def _arc_replace_from_t1(self, obj, eff_p):
        # Canonical ARC decision using effective p and B2 hint
        x_in_B2 = (obj.key in self.B2)
        t1_sz = len(self.T1)
        return (t1_sz >= 1) and (t1_sz > eff_p or (x_in_B2 and t1_sz == eff_p))

    def evict(self, cache_snapshot, obj):
        self._ensure_capacity(cache_snapshot)
        self._resync(cache_snapshot)
        self._idle_decay_p(cache_snapshot)

        # Canonical p update only on ghost hits, using ceil and capped steps
        if obj.key in self.B1 or obj.key in self.B2:
            if obj.key in self.B1:
                # step_up = ceil(|B2| / max(1, |B1|)), cap by C//8
                num, den = len(self.B2), max(1, len(self.B1))
                step_up = (num + den - 1) // den
                self.p = min(self.C, self.p + min(step_up, max(1, self.C // 8)))
            else:
                # step_down = ceil(|B1| / max(1, |B2|)), cap by C//8 (C//4 on long cold streaks)
                num, den = len(self.B1), max(1, len(self.B2))
                step_down = (num + den - 1) // den
                cap_step = max(1, self.C // 4) if self.cold_streak >= max(1, self.C // 2) else max(1, self.C // 8)
                dec = min(step_down, cap_step, self.p)
                self.p = max(0, self.p - dec)
            # record ghost-hit time and reset scan indicators
            self.last_ghost_hit_access = cache_snapshot.access_count
            self.cold_streak = 0
            self.scan_guard_until = -1
            self.bias_armed = False

        eff_p = self._effective_p(cache_snapshot)
        from_t1 = self._arc_replace_from_t1(obj, eff_p)

        # Primary ARC REPLACE
        if from_t1 and self.T1:
            self.last_replaced_from = 'T1'
            return next(iter(self.T1))
        if (not from_t1) and self.T2:
            self.last_replaced_from = 'T2'
            return next(iter(self.T2))

        # If preferred empty, try the other
        if self.T1:
            self.last_replaced_from = 'T1'
            return next(iter(self.T1))
        if self.T2:
            self.last_replaced_from = 'T2'
            return next(iter(self.T2))

        # Resync and retry once
        self._resync(cache_snapshot)
        from_t1 = self._arc_replace_from_t1(obj, eff_p)
        if from_t1 and self.T1:
            self.last_replaced_from = 'T1'
            return next(iter(self.T1))
        if (not from_t1) and self.T2:
            self.last_replaced_from = 'T2'
            return next(iter(self.T2))

        # Deterministic shallow fallback with unified peek budget
        d = min(8, max(1, self.C // 16))

        # Prefer: T1 LRU not in B2 (avoid evicting likely frequent)
        cnt = 0
        for k in self.T1.keys():
            if k not in self.B2:
                self.last_replaced_from = 'T1'
                return k
            cnt += 1
            if cnt >= d:
                break

        # Next: T2 LRU present in B1 (recency-only on T2)
        cnt = 0
        for k in self.T2.keys():
            if k in self.B1:
                self.last_replaced_from = 'T2'
                return k
            cnt += 1
            if cnt >= d:
                break

        # Timestamp tie-breaker over T1, else arbitrary
        if self.T1 and m_key_timestamp:
            best = None
            best_ts = float('inf')
            for k in self.T1.keys():
                ts = m_key_timestamp.get(k, float('inf'))
                if ts < best_ts:
                    best_ts = ts
                    best = k
            if best is not None:
                self.last_replaced_from = 'T1'
                return best

        # As a last resort, evict any cache key while trying to infer source
        if cache_snapshot.cache:
            for k in cache_snapshot.cache.keys():
                if k in self.T1:
                    self.last_replaced_from = 'T1'
                    return k
                if k in self.T2:
                    self.last_replaced_from = 'T2'
                    return k
            # unknown membership
            self.last_replaced_from = 'T1'
            return next(iter(cache_snapshot.cache.keys()))
        self.last_replaced_from = None
        return None

    def update_after_hit(self, cache_snapshot, obj):
        self._ensure_capacity(cache_snapshot)
        # Hit transitions: move to T2 MRU
        k = obj.key
        if k in self.T1:
            self.T1.pop(k, None)
            self._move_to_mru(self.T2, k)
        else:
            self._move_to_mru(self.T2, k)
        # Residents must be removed from ghosts
        self.B1.pop(k, None)
        self.B2.pop(k, None)
        # Timestamp and scan reset on reuse
        m_key_timestamp[k] = cache_snapshot.access_count
        self.cold_streak = 0
        self.scan_guard_until = -1
        self.bias_armed = False

    def update_after_insert(self, cache_snapshot, obj):
        self._ensure_capacity(cache_snapshot)
        k = obj.key
        # Admission: ghosts -> T2, brand-new -> T1
        if k in self.B1 or k in self.B2:
            # reuse detected: place in T2
            self.B1.pop(k, None)
            self.B2.pop(k, None)
            self._move_to_mru(self.T2, k)
            self.last_ghost_hit_access = cache_snapshot.access_count
            # reset scan guard/bias on reuse
            self.cold_streak = 0
            self.scan_guard_until = -1
            self.bias_armed = False
        else:
            # brand new -> T1
            self._move_to_mru(self.T1, k)
            self.cold_streak += 1
            # Start a short guard during potential scans/repeated cold phases
            if self.cold_streak >= max(1, self.C // 2):
                self.scan_guard_until = cache_snapshot.access_count + min(8, max(1, self.C // 16))
                self.bias_armed = True
        # maintain disjoint ghosts and timestamps
        self.B1.pop(k, None)
        self.B2.pop(k, None)
        m_key_timestamp[k] = cache_snapshot.access_count
        self._trim_ghosts()

    def update_after_evict(self, cache_snapshot, obj, evicted_obj):
        self._ensure_capacity(cache_snapshot)
        k = evicted_obj.key
        # Place victim into appropriate ghost using the remembered REPLACE source
        placed = False
        if self.last_replaced_from == 'T1':
            # Ensure it's removed from residents then move to B1
            self.T1.pop(k, None)
            self.B2.pop(k, None)
            self._move_to_mru(self.B1, k)
            placed = True
        elif self.last_replaced_from == 'T2':
            self.T2.pop(k, None)
            self.B1.pop(k, None)
            self._move_to_mru(self.B2, k)
            placed = True
        else:
            # Fallback to observed membership
            if k in self.T1:
                self.T1.pop(k, None)
                self.B2.pop(k, None)
                self._move_to_mru(self.B1, k)
                placed = True
            elif k in self.T2:
                self.T2.pop(k, None)
                self.B1.pop(k, None)
                self._move_to_mru(self.B2, k)
                placed = True

        if not placed:
            # Unknown: prefer consistency with existing ghost if any; otherwise B1
            if k in self.B2:
                self.B1.pop(k, None)
                self._move_to_mru(self.B2, k)
            else:
                self.B2.pop(k, None)
                self._move_to_mru(self.B1, k)

        # Clean up timestamp to contain growth
        m_key_timestamp.pop(k, None)
        self._trim_ghosts()
        # Reset remembered source for next eviction
        self.last_replaced_from = None


# Single global manager instance
_arc = ARCManager()


def evict(cache_snapshot, obj):
    return _arc.evict(cache_snapshot, obj)


def update_after_hit(cache_snapshot, obj):
    _arc.update_after_hit(cache_snapshot, obj)


def update_after_insert(cache_snapshot, obj):
    _arc.update_after_insert(cache_snapshot, obj)


def update_after_evict(cache_snapshot, obj, evicted_obj):
    _arc.update_after_evict(cache_snapshot, obj, evicted_obj)

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