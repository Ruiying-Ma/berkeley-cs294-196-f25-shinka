#!/usr/bin/env python3
"""Quick script to run EPLB evolution with gpt-5-mini and updated runtime scoring"""
import sys
sys.path.insert(0, '/Users/audreycc/Documents/Work/LLMTxn/ShinkaEvolve')

from shinka.core import EvolutionRunner, EvolutionConfig
from shinka.database import DatabaseConfig
from shinka.launch import LocalJobConfig
from examples.eplb.evaluate import main as evaluate_fn

job_config = LocalJobConfig(eval_program_path="examples/eplb/evaluate.py")

db_config = DatabaseConfig(
    db_path="evolution_db_eplb_runtime.sqlite",
    num_islands=2,
    archive_size=40,
    elite_selection_ratio=0.3,
    num_archive_inspirations=4,
    num_top_k_inspirations=2,
    migration_interval=10,
    migration_rate=0.1,
    island_elitism=True,
    parent_selection_strategy="weighted",
    parent_selection_lambda=10.0,
)

search_task_sys_msg = """You are an expert in parallel computing and load balancing algorithms, specializing in Mixture-of-Expert (MoE) models. The goal is to optimize the Expert Parallelism Load Balancer (EPLB) algorithm for vLLM.

This algorithm takes load metrics recorded by the vLLM server and rearranges experts to balance the load. It can replicate some experts to achieve better load balancing.

Your dual objectives are (EQUALLY IMPORTANT):
1. Improve load balancing efficiency - maximize the balancedness score by distributing expert loads evenly across GPUs
2. Improve computational efficiency - reduce the algorithm's execution time, as perfect load balancing is NP-hard

SCORING FORMULA (50% weight each):
- Balancedness score: average_load / max_load (higher is better, measures load distribution quality)
- Speed score: 0.02 / average_runtime (higher is better, measures algorithm speed)
- Combined score = 0.5 * balancedness_score + 0.5 * speed_score

CRITICAL: Fast algorithms are AS VALUABLE as perfectly balanced ones! A slightly less balanced but much faster algorithm may score higher overall. Optimize for BOTH metrics equally!

Key directions to explore (prioritize speed AND balancedness equally):
1. SPEED: Reduce computational overhead - minimize sorting, lookups, and iterations
2. SPEED: Use efficient data structures - lists instead of complex tensors where appropriate
3. SPEED: Add simple caching only when beneficial (avoid complex cache overhead)
4. SPEED: Consider approximation algorithms that trade slight optimality for major speed gains
5. BALANCE: The balanced_packing function can use better packing heuristics (greedy LPT works well)
6. BALANCE: The replicate_experts strategy could use smarter replication policies
7. BALANCE: The hierarchical balancing approach may benefit from alternative grouping strategies
8. TOPOLOGY: Explore GPU-aware placement strategies that account for NVLink topology
9. OPTIMIZATION: Consider dynamic programming or memoization for repeated subproblems (but watch overhead!)
10. EFFICIENCY: The sorting and indexing operations could be vectorized more efficiently

IMPORTANT: Avoid over-engineering! Simple, fast greedy heuristics often outperform complex optimization when speed is weighted equally.

The algorithm operates on workload tensors with shape [num_layers, num_logical_experts] and must produce valid mappings that respect hardware constraints (num_replicas, num_groups, num_nodes, num_gpus).

Make sure all tensor operations are correct and the output maintains proper structure for the vLLM inference engine.

Be creative and try to find new balancing strategies that outperform the baseline greedy hierarchical approach."""

evo_config = EvolutionConfig(
    task_sys_msg=search_task_sys_msg,
    patch_types=["diff", "full", "cross"],
    patch_type_probs=[0.6, 0.3, 0.1],
    num_generations=100,
    max_parallel_jobs=3,
    max_patch_resamples=3,
    max_patch_attempts=3,
    job_type="local",
    language="python",
    llm_models=["gpt-5-mini"],  # Only gpt-5-mini as requested
    llm_kwargs=dict(
        temperatures=[0.0, 0.5, 1.0],
        max_tokens=16384,
    ),
    meta_rec_interval=10,
    meta_llm_models=["gpt-5-mini"],
    meta_llm_kwargs=dict(temperatures=[0.0], max_tokens=8192),
    embedding_model="text-embedding-3-small",
    code_embed_sim_threshold=0.995,
    llm_dynamic_selection="ucb1",
    llm_dynamic_selection_kwargs=dict(exploration_coef=1.0),
    init_program_path="examples/eplb/initial.py",
    results_dir="results_eplb_runtime_opt",
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

