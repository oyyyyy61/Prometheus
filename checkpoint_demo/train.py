from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import torch
from torch import nn


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train a tiny model and resume automatically from a checkpoint."
    )
    parser.add_argument("--epochs", type=int, default=20, help="Total epoch count.")
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=Path(__file__).with_name("checkpoint.pt"),
        help="Checkpoint file path.",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=1.0,
        help="Pause after each epoch so there is time to interrupt the demo.",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Delete this demo's checkpoint before training.",
    )
    return parser.parse_args()


def make_training_data() -> tuple[torch.Tensor, torch.Tensor]:
    generator = torch.Generator().manual_seed(2026)
    features = torch.randn(256, 1, generator=generator)
    noise = torch.randn(256, 1, generator=generator) * 0.1
    targets = 3.0 * features + 2.0 + noise
    return features, targets


def save_checkpoint(
    path: Path,
    model: nn.Module,
    optimizer: torch.optim.Optimizer,
    next_epoch: int,
    loss: float,
) -> None:
    checkpoint = {
        "schema_version": 1,
        "next_epoch": next_epoch,
        "model_state_dict": model.state_dict(),
        "optimizer_state_dict": optimizer.state_dict(),
        "loss": loss,
        "torch_rng_state": torch.get_rng_state(),
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    torch.save(checkpoint, temporary_path)
    os.replace(temporary_path, path)


def load_checkpoint(
    path: Path,
    model: nn.Module,
    optimizer: torch.optim.Optimizer,
) -> int:
    if not path.exists():
        print("[start] No checkpoint found. Starting from epoch 0.", flush=True)
        return 0

    checkpoint = torch.load(path, map_location="cpu", weights_only=True)
    if checkpoint.get("schema_version") != 1:
        raise RuntimeError("Unsupported checkpoint schema version.")

    model.load_state_dict(checkpoint["model_state_dict"])
    optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
    torch.set_rng_state(checkpoint["torch_rng_state"])

    next_epoch = int(checkpoint["next_epoch"])
    previous_loss = float(checkpoint["loss"])
    print(
        f"[resume] Loaded {path}. Next epoch: {next_epoch}; "
        f"previous loss: {previous_loss:.6f}",
        flush=True,
    )
    return next_epoch


def main() -> None:
    args = parse_args()
    if args.epochs < 1:
        raise ValueError("--epochs must be at least 1")
    if args.sleep_seconds < 0:
        raise ValueError("--sleep-seconds cannot be negative")

    if args.fresh and args.checkpoint.exists():
        args.checkpoint.unlink()
        print(f"[fresh] Removed {args.checkpoint}", flush=True)

    torch.manual_seed(7)
    features, targets = make_training_data()

    model = nn.Linear(1, 1)
    optimizer = torch.optim.SGD(model.parameters(), lr=0.1)
    loss_function = nn.MSELoss()
    start_epoch = load_checkpoint(args.checkpoint, model, optimizer)

    if start_epoch >= args.epochs:
        print(
            f"[done] Checkpoint already reached {start_epoch} epochs. "
            "Increase --epochs or use --fresh.",
            flush=True,
        )
        return

    try:
        for epoch in range(start_epoch, args.epochs):
            optimizer.zero_grad()
            predictions = model(features)
            loss = loss_function(predictions, targets)
            loss.backward()
            optimizer.step()

            completed_epochs = epoch + 1
            save_checkpoint(
                args.checkpoint,
                model,
                optimizer,
                next_epoch=completed_epochs,
                loss=loss.item(),
            )
            print(
                f"[epoch {completed_epochs:03d}/{args.epochs:03d}] "
                f"loss={loss.item():.6f}; checkpoint saved",
                flush=True,
            )
            time.sleep(args.sleep_seconds)
    except KeyboardInterrupt:
        print(
            "\n[stopped] Interrupted. The most recently completed epoch is safe.",
            flush=True,
        )
        return

    weight = model.weight.item()
    bias = model.bias.item()
    print(f"[done] Learned y = {weight:.3f} * x + {bias:.3f}", flush=True)


if __name__ == "__main__":
    main()
