import os
import random
random.seed(42) # set the random seed before importing `My` to enable reproduction
import My
import importlib
import struct
from typing import List
import numpy as np


class TraceEntry:
    def __init__(self, time: int, key: int, size: int, next_vtime: int):
        self.time = time
        self.key = key
        self.size = size
        self.next_vtime = next_vtime

    @classmethod
    def from_bin(cls, data: bytes):
        s = struct.Struct("<IQIq")
        return TraceEntry(*s.unpack(data))

    def to_bin(self):
        s = struct.Struct("<IQIq")
        return s.pack(self._signed_2_unsigned(4, int(self.time)), 
                      self._signed_2_unsigned(8, int(self.key)), 
                      self._signed_2_unsigned(4, int(self.size)), 
                      int(self.next_vtime))
    @classmethod
    def from_csv(cls, row: str):
        row = row.strip().split(",")
        return TraceEntry(int(row[0]), int(row[1]), int(row[2]), int(row[3]))

    def to_csv(self):
        return f"{self.time},{self.key},{self.size},{self.next_vtime}"

    def __str__(self):
        return f"({self.time}, {self.key}, {self.size}, {self.next_vtime})"

    def __repr__(self):
        return self.__str__()

    def _signed_2_unsigned(self, byte, x):
        assert isinstance(x, int)
        if byte == 4:
            return x & 0xffffffff
        elif byte == 8:
            return x & 0xffffffffffffffff
        else:
            raise ValueError

class Trace:
    def __init__(self, trace_path: str, next_vtime_set: bool = True):
        self.entries: List[TraceEntry] = []
        if trace_path.endswith(".bin"):
            s = struct.Struct("<IQIq")
            with open(trace_path, "rb") as f:
                while True:
                    data = f.read(s.size)
                    if not data:
                        break
                    trace_entry = TraceEntry.from_bin(data)
                    self.entries.append(trace_entry)
        elif trace_path.endswith(".csv"):
            with open(trace_path, "r") as f:
                for line in f:
                    trace_entry = TraceEntry.from_csv(line)
                    self.entries.append(trace_entry)
        if next_vtime_set == False:
            self.set_next_vtime()

    def get_key_set(self, range_s: int=None, range_e: int=None):
        '''
        [range_s, range_e)
        '''
        if range_s == None and range_e == None:
            return set([entry.key for entry in self.entries])
        elif range_s != None:
            range_s = np.clip(range_s, 0, self.get_len() - 1)
            return set([entry.key for entry in self.entries[range_s:]])
        elif range_e != None:
            range_e = np.clip(range_e, 0, self.get_len())
            return set([entry.key for entry in self.entries[:range_e]])
        range_s = np.clip(range_s, 0, self.get_len() - 1)
        range_e = np.clip(range_e, 0, self.get_len())
        if range_s >= range_e:
            return set()
        return set([entry.key for entry in self.entries[range_s:range_e]])

    def get_ndv(self, range_s: int=None, range_e: int=None):
        '''
        [range_s, range_e)
        '''
        return len(self.get_key_set(range_s=range_s, range_e=range_e))
    
    def get_len(self):
        return len(self.entries)
    
    def set_next_vtime(self):
        m_key_vtime = {}
        for entry in self.entries[::-1]:
            if entry.key in m_key_vtime:
                entry.next_vtime = m_key_vtime[entry.key]
            else:
                entry.next_vtime = -1
            m_key_vtime[entry.key] = entry.time
    
    def to_bin(self, path: str, start=None, end=None):
        if start == None or start < 0:
            start = 0
        if end == None or end > len(self.entries):
            end = len(self.entries)
        with open(path, "wb") as f:
            for entry in self.entries[start:end]:
                f.write(entry.to_bin())

    def to_csv(self, path: str, start=None, end=None):
        if start == None or start < 0:
            start = 0
        if end == None or end > len(self.entries):
            end = len(self.entries)
        with open(path, "w") as f:
            for entry in self.entries[start:end]:
                f.write(entry.to_csv() + "\n")

class CacheObj:
    def __init__(self, key, size=1, consider_obj_size=False):
        if not isinstance(key, str):
            raise ValueError("KEY must be a string.")
        if not isinstance(size, int) or not size > 0:
            raise ValueError("SIZE must be a positive integer.")
        
        self.__key = key
        self.__size = size if consider_obj_size == True else 1 # size in bytes

    @property
    def size(self): # read-only
        return self.__size
    
    @property
    def key(self): # read-only
        return self.__key
    
class CacheConfig:
    def __init__(self, capacity: int, consider_obj_size: bool=False, key_col_id=1, size_col_id=2, has_header: bool=False, delimiter=","):
        if not isinstance(capacity, int) or not capacity > 0:
            raise ValueError("CAPACITY must be a positive integer.")
        
        if not isinstance(consider_obj_size, bool):
            raise ValueError("CONSIDER_OBJ_SIZE msut be a boolean value.")
        
        
        self.capacity = capacity
        self.consider_obj_size = consider_obj_size
        # parameters for trace
        self.key_col_id = key_col_id
        self.size_col_id = size_col_id
        self.has_header = has_header
        self.delimiter = delimiter
    
    def to_dict(self) -> dict:
        return {
            "capacity": self.capacity,
            "consider_obj_size": self.consider_obj_size,
            "key_col_id": self.key_col_id,
            "size_col_id": self.size_col_id,
            "has_header": self.has_header,
            "delimiter": self.delimiter
        }
    
class Cache:
    def __init__(self, config: CacheConfig):
        assert isinstance(config, CacheConfig)
       
        self.__capacity = config.capacity
        self.__cache = dict() # a map from key to cache_obj
        self.__naccess = 0
        self.__nhit = 0
        importlib.reload(My)
        self.update_after_insert_func = My.update_after_insert
        self.update_after_evict_func = My.update_after_evict
        self.update_after_hit_func = My.update_after_hit
        self.evict_func = My.evict
    
    @property
    def cache(self): # read-only
        return self.__cache
    
    @property
    def size(self): # read-only
        tot_size = 0
        for obj in self.__cache.values():
            assert isinstance(obj, CacheObj)
            obj_size = obj.size
            assert isinstance(obj_size, int) and obj_size > 0
            tot_size += obj_size
        return tot_size
    
    @property
    def capacity(self): # read-only
        return self.__capacity
    
    @property
    def access_count(self):
        return self.__naccess
    
    @property
    def hit_count(self):
        return self.__nhit
    
    @property
    def miss_count(self):
        return self.__naccess - self.__nhit
    
    @property
    def snapshot(self): # read-only
        return self


    def get(self, obj) -> bool: # never exposed to LLM
        self.__naccess += 1
        
        if not isinstance(obj, CacheObj):
            raise ValueError("OBJ must be an instance of CacheObj")

        if obj.key in self.cache:
            # hit, return true
            # update
            self.__nhit += 1
            self.update_after_hit(obj)
            return True
        else:
            # miss, return False
            if not self.can_insert(obj):
                return False
            if not self.admit(obj):
                return False
            while self.size + obj.size > self.capacity:
                evicted_cache_object = self.evict(obj)
                self.update_after_evict(obj, evicted_cache_object)
            assert self.size + obj.size <= self.capacity
            self.insert(obj)
            self.update_after_insert(obj)
            return False
            
        
    def update_after_hit(self, obj): # never exposed to LLM
        if not isinstance(obj, CacheObj):
            raise ValueError("OBJ must be an instance of CacheObj.")
        if not obj.key in self.__cache:
            raise ValueError("OBJ must be in cache after hit.")

        self.update_after_hit_func(self.snapshot, obj)

    def update_after_insert(self, obj): # never exposed to LLM
        if not isinstance(obj, CacheObj):
            raise ValueError("OBJ must be an instance of CacheObj.")
        if not obj.key in self.__cache:
            raise ValueError("OBJ must be in cache after insert.")
        
        self.update_after_insert_func(self.snapshot, obj)
    
    def update_after_evict(self, obj, evicted_obj): # never exposed to LLM
        if not isinstance(obj, CacheObj):
            raise ValueError("OBJ must be an instance of CacheObj.")
        if obj.key in self.__cache:
            raise ValueError("OBJ must not be in cache before eviction completes.")
        if evicted_obj != None:
            if not isinstance(evicted_obj, CacheObj):
                raise ValueError("EVICTED_OBJ must be an instance of CacheObj if not None.")
            if evicted_obj.key in self.__cache:
                raise ValueError("EVICTED_OBJ must not be in cache after eviction.")
        else:
            raise ValueError("EVICTIED_OBJ must not be None.")
        
        self.update_after_evict_func(self.snapshot, obj, evicted_obj)
    
    def evict(self, obj): # never exposed to LLM
        '''
        Return:
        - evicted_cache_obj (CacheObj): the evicted cache object.
        '''
        candid_obj_key = self.evict_func(self.snapshot, obj)
        if candid_obj_key == None or candid_obj_key not in self.__cache:
            raise ValueError("CANDID_OBJ_KEY must be in cache")
        assert candid_obj_key != None
        assert candid_obj_key in self.__cache
        candid_obj_size = self.__cache[candid_obj_key].size
        old_size = self.size
        evicted_cache_obj = self.__cache.pop(candid_obj_key)
        new_size = self.size
        assert new_size == old_size - candid_obj_size
        return evicted_cache_obj

    def insert(self, obj): # never exposed to LLM
        assert obj.key not in self.__cache
        old_size = self.size
        obj_size = obj.size
        self.__cache[obj.key] = obj
        new_size = self.size
        assert old_size + obj_size == new_size

    def can_insert(self, obj): # never exposed to LLM
        if obj.size > self.capacity:
            return False
        return True
    
    def admit(self, obj): # never exposed to LLM
        should_admit = (self.capacity >= obj.size)
        assert isinstance(should_admit, bool)
        return should_admit
