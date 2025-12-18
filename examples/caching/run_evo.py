from shinka.core import EvolutionRunner, EvolutionConfig
from shinka.database import DatabaseConfig
from shinka.launch import LocalJobConfig
import os

LLM_MODEL = "gemini-3-pro-preview"
PARALLEL = 1
NUM_ITER = 2

cur_folder = os.path.dirname(os.path.abspath(__file__))
evaluate_py = os.path.join(cur_folder, "evaluate.py")
initial_py = os.path.join(cur_folder, "initial.py")
result_folder = os.path.join(cur_folder, "shinka_results")
os.makedirs(result_folder, exist_ok=True)
assert os.path.exists(evaluate_py), "evaluate.py not found"
assert os.path.exists(initial_py), "initial.py not found"

job_config = LocalJobConfig(eval_program_path=evaluate_py, time="00:05:00")

strategy = "weighted"
if strategy == "uniform":
    # 1. Uniform from correct programs
    parent_config = dict(
        parent_selection_strategy="power_law",
        exploitation_alpha=0.0,
        exploitation_ratio=1.0,
    )
elif strategy == "hill_climbing":
    # 2. Hill Climbing (Always from the Best)
    parent_config = dict(
        parent_selection_strategy="power_law",
        exploitation_alpha=100.0,
        exploitation_ratio=1.0,
    )
elif strategy == "weighted":
    # 3. Weighted Prioritization
    parent_config = dict(
        parent_selection_strategy="weighted",
        parent_selection_lambda=10.0,
    )
elif strategy == "power_law":
    # 4. Power-Law Prioritization
    parent_config = dict(
        parent_selection_strategy="power_law",
        exploitation_alpha=1.0,
        exploitation_ratio=0.2,
    )
elif strategy == "power_law_high":
    # 5. Power-Law Prioritization (High Alpha)
    parent_config = dict(
        parent_selection_strategy="power_law",
        exploitation_alpha=2.0,
        exploitation_ratio=0.2,
    )
elif strategy == "beam_search":
    # 6. Beam Search
    parent_config = dict(
        parent_selection_strategy="beam_search",
        num_beams=10,
    )


db_config = DatabaseConfig(
    db_path="evolution_db.sqlite",
    num_islands=5,
    archive_size=20,
    # Inspiration parameters
    elite_selection_ratio=0.3,
    num_archive_inspirations=4,
    num_top_k_inspirations=2,
    # Island migration parameters
    migration_interval=5,
    migration_rate=0.1,  # chance to migrate program to random island
    island_elitism=True,  # Island elite is protected from migration
    **parent_config,
)

search_task_sys_msg = """You are an expert in computer system caching. 
Only change code within EVOLVE-BLOCK-START and EVOLVE-BLOCK-END.
Your task is to improve a cache eviction algorithm to minimize the cache miss rate. The cache receives a sequence of access requests for objects, and when the cache is full, it must evict an object to make space for a new one. The cache is full when the total number of cached objects reaches its capacity. Focus on improving the `evict` function, the `update_after_hit` function, the `update_after_insert` function, and the `update_after_evict` function to find a cache eviction algorithm with as low miss rate as possible.

**TASK:** Improve the `evict` function, the `update_after_hit` function, the `update_after_insert` function, and the `update_after_evict` function to find optimal cache eviction algorithms that minimize miss rates.

**PROBLEM SPECIFICS:**
- **Input:** A sequence of cache access requests for objects
- **Goal:** Find a cache eviction algorithm that minimizes the total number of cache misses
- **Constraints:** The number of objects that can be stored in the cache is limited by its capacity.

**INSTRUCTIONS:**
Focus on evolving the `evict` function, the `update_after_hit` function, the `update_after_insert` function to produce the best algorithm possible with the minimal miss rate.
- `evict` defines how the algorithm chooses the eviction victim.
- `update_after_hit` defines how the algorithm update the metadata it maintains immediately after a cache hit.
- `update_after_insert` defines how the algorithm updates the metadata it maintains immediately after inserting a new object into the cache.
- `update_after_evict` defines how the algorithm updates the metadata it maintains immediately after evicting the victim.
You have read-only access to these data and no access to any functions:
- An "object" represents the unit of a request, such as inserting an object into the cache or retrieving an object from the cache. Each object `obj` provides the following **read-only** attributes that you can reference:
    - `obj.key` (str): A string that uniquely identifies the object.
    - `obj.size` (int): A positive integer representing the size of the object in bytes.
- You can also reference the following **read-only** attributes provided by a cache snapshots `cache_snapshot`:
    - `cache_snapshot.cache` (dict): A dictionary containing the cached objects, where the keys are the objects' keys, and the values are the corresponding objects themselves.
    - `cache_snapshot.size` (int): A non-negative integer representing the current total size of the cache in bytes.
    - `cache_snapshot.capacity` (int): A positive integer representing the maximum allowed size of the cache in bytes.
    - `cache_snapshot.access_count` (int): The current total number of cache accesses. You can also use this to represent current time.
    - `cache_snapshot.hit_count` (int): The current total number of cache hits.
    - `cache_snapshot.miss_count` (int): The current total number of cache misses.

Explain step-by-step the reasoning process for your solution and how this will lead to a better cache eviction algorithm.
"""

evo_config = EvolutionConfig(
    task_sys_msg=search_task_sys_msg,
    patch_types=["diff", "full", "cross"],
    patch_type_probs=[0.6, 0.3, 0.1],
    num_generations=NUM_ITER,
    max_parallel_jobs=PARALLEL,
    max_patch_resamples=3,
    max_patch_attempts=3,
    job_type="local",
    language="python",
    llm_models=[
        LLM_MODEL,
    ],
    llm_kwargs=dict(
        temperatures=[0.0, 0.7, 1.0],
        reasoning_efforts=["auto"],
        max_tokens=32768,
    ),
    meta_rec_interval=10,
    meta_llm_models=[LLM_MODEL],
    meta_llm_kwargs=dict(temperatures=[0.7], max_tokens=16384),
    embedding_model="text-embedding-3-small",
    code_embed_sim_threshold=0.995,
    novelty_llm_models=[LLM_MODEL],
    novelty_llm_kwargs=dict(temperatures=[0.7], max_tokens=16384),
    llm_dynamic_selection="ucb1",
    llm_dynamic_selection_kwargs=dict(exploration_coef=1.0),
    init_program_path=initial_py,
    results_dir=result_folder,
)


def main():
    evo_runner = EvolutionRunner(
        evo_config=evo_config,
        job_config=job_config,
        db_config=db_config,
        verbose=True,
    )
    evo_runner.run()


if __name__ == "__main__":
    results_data = main()
