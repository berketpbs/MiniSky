"""Placeholder distributed training script for examples/distributed-training.yaml.

Reads the rank/world-size environment variables torchrun sets, so the example
shows where a real distributed entrypoint would plug in.
"""

import os

rank = os.environ.get("RANK", "0")
world_size = os.environ.get("WORLD_SIZE", "1")
print(f"rank {rank} of {world_size} starting")
print(f"rank {rank} wrote checkpoint shard")
