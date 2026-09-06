"""Placeholder evaluation script for examples/train-model.yaml."""

import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--checkpoint", default="best_model.pt")
args = parser.parse_args()

print(f"evaluating {args.checkpoint}")
print("accuracy: 0.0 (placeholder)")
