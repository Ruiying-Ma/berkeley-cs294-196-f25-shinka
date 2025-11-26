"""
Evaluator for transaction scheduling example with improved timeout handling
"""

import os
import argparse
import numpy as np
from typing import Tuple, Optional, List, Dict, Any

from shinka.core import run_shinka_eval


def format_schedules_string(schedules: List[List[int]]) -> str:
    """Formats transaction schedules into a multi-line string for display."""
    result = []
    for i, schedule in enumerate(schedules):
        result.append(f"  workload_{i+1}_schedule = {schedule[:10]}{'...' if len(schedule) > 10 else ''}")
    return "\n".join(result)


def adapted_validate_scheduling(
    run_output: Tuple[float, List[List[int]], float],
    atol=1e-6,
) -> Tuple[bool, Optional[str]]:
    """
    Validates transaction scheduling results based on the output of 'run_scheduling'.

    Args:
        run_output: Tuple (total_makespan, schedules, execution_time) from run_scheduling.

    Returns:
        (is_valid: bool, error_message: Optional[str])
    """
    total_makespan, schedules, execution_time = run_output
    msg = "The schedules are valid. All transactions are properly ordered and no duplicates exist."
    
    if not isinstance(schedules, list):
        msg = f"Schedules should be a list, got {type(schedules)}"
        return False, msg
    
    if len(schedules) != 3:
        msg = f"Expected 3 schedules (one per workload), got {len(schedules)}"
        return False, msg
    
    # Validate each schedule
    for i, schedule in enumerate(schedules):
        if not isinstance(schedule, list):
            msg = f"Schedule {i} should be a list, got {type(schedule)}"
            return False, msg
        
        if len(schedule) != 100:  # Each workload has 100 transactions
            msg = f"Schedule {i} should have 100 transactions, got {len(schedule)}"
            return False, msg
        
        # Check for duplicates
        if len(set(schedule)) != len(schedule):
            msg = f"Schedule {i} contains duplicate transaction IDs"
            return False, msg
        
        # Check that all transaction IDs are present (0-99)
        expected_ids = set(range(100))
        actual_ids = set(schedule)
        if expected_ids != actual_ids:
            missing = expected_ids - actual_ids
            extra = actual_ids - expected_ids
            msg = f"Schedule {i} has missing IDs: {missing} or extra IDs: {extra}"
            return False, msg
    
    # Validate makespan is reasonable
    if total_makespan < 0:
        msg = f"Total makespan should be non-negative, got {total_makespan}"
        return False, msg
    
    if execution_time < 0:
        msg = f"Execution time should be non-negative, got {execution_time}"
        return False, msg
    
    return True, msg


def get_txn_scheduling_kwargs(run_index: int) -> Dict[str, Any]:
    """Provides keyword arguments for transaction scheduling runs (none needed)."""
    return {}


def aggregate_txn_scheduling_metrics(
    results: List[Tuple[float, List[List[int]], float]], results_dir: str
) -> Dict[str, Any]:
    """
    Aggregates metrics for transaction scheduling. Assumes num_runs=1.
    Saves extra.npz with detailed scheduling information.
    """
    if not results:
        return {"combined_score": 0.0, "error": "No results to aggregate"}

    total_makespan, schedules, execution_time = results[0]

    public_metrics = {
        "schedules_str": format_schedules_string(schedules),
        "num_workloads": len(schedules),
        "total_transactions": sum(len(schedule) for schedule in schedules),
    }
    private_metrics = {
        "total_makespan": float(total_makespan),
        "execution_time": float(execution_time),
        "individual_makespans": [float(len(schedule)) for schedule in schedules],  # Placeholder
    }
    metrics = {
        "combined_score": float(1000 / (1 + total_makespan)) if total_makespan > 0 else 0.0,  # Higher score for lower makespan
        "public": public_metrics,
        "private": private_metrics,
    }

    extra_file = os.path.join(results_dir, "extra.npz")
    try:
        np.savez(
            extra_file,
            total_makespan=total_makespan,
            schedules=schedules,
            execution_time=execution_time,
        )
        print(f"Detailed scheduling data saved to {extra_file}")
    except Exception as e:
        print(f"Error saving extra.npz: {e}")
        metrics["extra_npz_save_error"] = str(e)

    return metrics


def main(program_path: str, results_dir: str):
    """Runs the transaction scheduling evaluation using shinka.eval."""
    print(f"Evaluating program: {program_path}")
    print(f"Saving results to: {results_dir}")
    os.makedirs(results_dir, exist_ok=True)

    num_experiment_runs = 1

    # Define a nested function to pass results_dir to the aggregator
    def _aggregator_with_context(
        r: List[Tuple[float, List[List[int]], float]],
    ) -> Dict[str, Any]:
        return aggregate_txn_scheduling_metrics(r, results_dir)

    metrics, correct, error_msg = run_shinka_eval(
        program_path=program_path,
        results_dir=results_dir,
        experiment_fn_name="run_scheduling",
        num_runs=num_experiment_runs,
        get_experiment_kwargs=get_txn_scheduling_kwargs,
        validate_fn=adapted_validate_scheduling,
        aggregate_metrics_fn=_aggregator_with_context,
    )

    if correct:
        print("Evaluation and Validation completed successfully.")
    else:
        print(f"Evaluation or Validation failed: {error_msg}")

    print("Metrics:")
    for key, value in metrics.items():
        if isinstance(value, str) and len(value) > 100:
            print(f"  {key}: <string_too_long_to_display>")
        else:
            print(f"  {key}: {value}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Transaction scheduling evaluator using shinka.eval"
    )
    parser.add_argument(
        "--program_path",
        type=str,
        default="initial.py",
        help="Path to program to evaluate (must contain 'run_scheduling')",
    )
    parser.add_argument(
        "--results_dir",
        type=str,
        default="results",
        help="Dir to save results (metrics.json, correct.json, extra.npz)",
    )
    parsed_args = parser.parse_args()
    main(parsed_args.program_path, parsed_args.results_dir)
