"""Placeholder training script for examples/train-model.yaml.

Stands in for a real training run so the example is launchable as-is; swap
this directory for your own code.
"""

import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--epochs", type=int, default=1)
parser.add_argument("--batch-size", type=int, default=32)
args = parser.parse_args()

for epoch in range(args.epochs):
    print(f"epoch {epoch + 1}/{args.epochs} batch_size={args.batch_size}")
print("saved best_model.pt")
