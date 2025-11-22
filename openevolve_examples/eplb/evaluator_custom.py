import functools
import importlib.util
import json
import time
import traceback
import os
from typing import TypedDict

import torch

# Try the specified path first, fall back to local if not found
WORKLOAD_PATH_REMOTE = "/home/bowen/vllm/expert-load/expert-load-20250627_103226-1200.json"
WORKLOAD_PATH_LOCAL = os.path.join(os.path.dirname(__file__), "expert-load.json")

# Use remote if it exists, otherwise use local
if os.path.exists(WORKLOAD_PATH_REMOTE):
    WORKLOAD_PATH = WORKLOAD_PATH_REMOTE
    print(f"Using remote workload: {WORKLOAD_PATH}")
else:
    WORKLOAD_PATH = WORKLOAD_PATH_LOCAL
    print(f"Remote workload not found, using local: {WORKLOAD_PATH}")

REBALANCE_INTERVAL = 100

NUM_REPLICAS = 288
NUM_GROUPS = 8
NUM_GPUS = 32
NUM_NODES = 4

@functools.cache
def load_workloads(path: str) -> list[torch.Tensor]: 
    with open(path, "r") as f:
        data = json.load(f)

    total_len = len(data['load_history'])
    workloads = []
    for i in range(0, total_len, REBALANCE_INTERVAL):
        start = i
        end = min(start + REBALANCE_INTERVAL, total_len)

        load = torch.tensor([x['logical_expert_load'] for x in data['load_history'][start:end]]).sum(dim=0)
        workloads.append(load)

    return workloads

class EvaluationResult(TypedDict, total=False):
    balancedness_score: float
    speed_score: float
    combined_score: float
    error: str

def simulate_inference(log2phy: torch.Tensor, logcnt: torch.Tensor, workload: torch.Tensor) -> float:
    '''
    Simulate a MoE inference with the given expert mapping, and return the balancedness factor.
    '''
    # workload 形状: (num_layers, num_logical_experts) - 每层每个逻辑专家的负载
    num_layers, num_logical_experts = workload.shape
    
    # 初始化物理专家负载累积器
    num_physical_experts = NUM_REPLICAS
    total_physical_load = torch.zeros(num_layers, num_physical_experts, dtype=torch.float, device=workload.device)
    
    # 对每个逻辑专家，分配负载到其物理副本
    for layer_id in range(num_layers):
        for logical_id in range(num_logical_experts):
            # 获取该逻辑专家的负载
            logical_load = workload[layer_id][logical_id].item()
            
            # 跳过零负载
            if logical_load <= 0:
                continue
                
            num_replicas = int(logcnt[layer_id][logical_id].item())

            # 跳过零副本
            if num_replicas <= 0:
                continue

            # 获取物理专家映射
            physical_ids = log2phy[layer_id][logical_id][:num_replicas]
                
            # 计算每个副本的负载（基于有效副本数量）
            replica_load = logical_load / num_replicas
            
            # 分配负载到有效的物理专家
            total_physical_load[layer_id, physical_ids] += replica_load
    
    # 计算 balancedness
    total_load = total_physical_load.sum()
    if total_load == 0:
        return 0.0
    
    # 计算每层的平均负载和最大负载，然后求和
    layer_avg = total_physical_load.mean(dim=1)  # (num_layers,)
    layer_max = total_physical_load.max(dim=1).values  # (num_layers,)
    
    avg_load = layer_avg.sum().item()
    max_load = layer_max.sum().item()
    
    # 计算 balancedness: avg_load / max_load
    balancedness = avg_load / max_load if max_load > 0 else 0.0
    
    print(f'balancedness: {balancedness}')
    
    return balancedness

def evaluate(program_path: str) -> EvaluationResult:
    workloads = load_workloads(WORKLOAD_PATH)

    try:
        spec = importlib.util.spec_from_file_location("program", program_path)
        assert spec is not None
        program = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(program)

        if not hasattr(program, "rebalance_experts"):
            print('Error: program does not have `rebalance_experts` function')
            return {
                "balancedness_score": 0.0,
                "speed_score": 0.0,
                "combined_score": 0.0,
                "error": "Missing `rebalance_experts` function",
            }

        if not hasattr(program, "rebalance_experts"):
            raise ValueError("Program does not have rebalance_experts function")
        
        balancedness_scores = []
        times = []
        for i in range(len(workloads) - 1):
            start_time = time.perf_counter()
            _, log2phy, logcnt = program.rebalance_experts(
                workloads[i],
                NUM_REPLICAS,
                NUM_GROUPS,
                NUM_NODES,
                NUM_GPUS,
            )
            balancedness_score = simulate_inference(log2phy, logcnt, workloads[i + 1])
            end_time = time.perf_counter()
            balancedness_scores.append(balancedness_score)
            times.append(end_time - start_time)
        avg_balancedness_score = sum(balancedness_scores) / len(balancedness_scores)
        avg_time = sum(times) / len(times)
        speed_score = 0.02 / avg_time
        print(f'avg_time: {avg_time}, speed_score: {speed_score}')
        combined_score = (avg_balancedness_score + speed_score) / 2
        return {
            "balancedness_score": float(avg_balancedness_score),
            "speed_score": float(speed_score),
            "combined_score": float(combined_score),
        }
    except Exception as e:
        traceback.print_exc()
        print(f'Error during evaluation: {str(e)}')
        return {
            "balancedness_score": 0.0,
            "speed_score": 0.0,
            "combined_score": 0.0,
            "error": str(e),
        }
    
    return {
        "balancedness_score": 0.0,
        "speed_score": 0.0,
        "combined_score": 0.0,
        "error": "No error",
    }


if __name__ == "__main__":
    import sys
    
    programs = [
        ('initial_program.py', 'Baseline'),
        ('best_shinka_program.py', 'Best v1'),
        ('best_shinka_program_2.py', 'Best v2 (new)'),
    ]
    
    print('=' * 80)
    print('COMPARATIVE EVALUATION WITH CUSTOM WORKLOAD')
    print('=' * 80)
    print()
    
    results = []
    for prog_path, prog_name in programs:
        if not os.path.exists(prog_path):
            print(f'Warning: {prog_path} not found, skipping...')
            continue
        print(f'Evaluating {prog_name}...')
        result = evaluate(prog_path)
        results.append((prog_name, result))
        print()
    
    print('=' * 80)
    print('COMPARISON TABLE')
    print('=' * 80)
    print(f"{'Program':<20} {'Balancedness':>15} {'Speed':>12} {'Combined':>12}")
    print('-' * 80)
    for prog_name, result in results:
        bal = result.get('balancedness_score', 0)
        speed = result.get('speed_score', 0)
        combined = result.get('combined_score', 0)
        print(f'{prog_name:<20} {bal:>15.6f} {speed:>12.6f} {combined:>12.6f}')
    print('=' * 80)
    
    # Show improvement
    if len(results) >= 2:
        baseline_score = results[0][1].get('combined_score', 0)
        for i in range(1, len(results)):
            prog_name = results[i][0]
            score = results[i][1].get('combined_score', 0)
            if baseline_score > 0:
                improvement = ((score - baseline_score) / baseline_score) * 100
                print(f'{prog_name} vs Baseline: {improvement:+.2f}%')

