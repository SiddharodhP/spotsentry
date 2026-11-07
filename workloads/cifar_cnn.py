"""A real machine-learning job: a small CNN on CIFAR-10, on CPU."""

from __future__ import annotations

import random
from collections.abc import Iterator
from typing import Any

import numpy as np
import torch
from torch import nn
from torch.utils.data import TensorDataset

from spotsentry.adapters.torch_trainer import TorchTrainerAdapter
from spotsentry.workload import Workload

_MEAN = torch.tensor([0.4914, 0.4822, 0.4465]).view(1, 3, 1, 1)
_STD = torch.tensor([0.2470, 0.2435, 0.2616]).view(1, 3, 1, 1)


class SmallCNN(nn.Module):
    """Three convolution blocks and a dropout classifier."""

    def __init__(self, width: int = 32, classes: int = 10) -> None:
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(3, width, 3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Conv2d(width, width * 2, 3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Conv2d(width * 2, width * 4, 3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2),
        )
        self.classifier = nn.Sequential(
            nn.Flatten(),
            nn.Dropout(0.25),
            nn.Linear(width * 4 * 4 * 4, 256),
            nn.ReLU(),
            nn.Linear(256, classes),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.classifier(self.features(x))


def _prepare(images: np.ndarray, labels: list[int] | np.ndarray) -> TensorDataset:
    tensor = torch.from_numpy(images).permute(0, 3, 1, 2).float() / 255.0
    return TensorDataset((tensor - _MEAN) / _STD, torch.as_tensor(labels, dtype=torch.long))


def _fake_data(count: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """Random images whose mean colour depends on the label, so tests need no download."""
    rng = np.random.default_rng(seed)
    labels = rng.integers(0, 10, size=count)
    images = rng.integers(0, 120, size=(count, 32, 32, 3), dtype=np.uint8)
    images[..., 0] += (labels * 13).astype(np.uint8)[:, None, None]
    return images, labels


class CifarCNN(Workload):
    """Trains ``SmallCNN``. Dropout and random flips make it depend on saved random state."""

    def __init__(
        self,
        epochs: int = 20,
        batch_size: int = 128,
        lr: float = 0.001,
        width: int = 32,
        seed: int = 42,
        deterministic: bool = False,
        augment: bool = True,
        dataset: str = "cifar10",
        data_dir: str = "data",
        subset: int | None = None,
        test_subset: int | None = None,
        num_threads: int | None = None,
    ) -> None:
        self.epochs = epochs
        self.batch_size = batch_size
        self.augment = augment
        if num_threads:
            torch.set_num_threads(num_threads)
        if deterministic:
            torch.use_deterministic_algorithms(True)
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)

        train, test = self._load(dataset, data_dir, seed)
        if subset:
            train = TensorDataset(*(t[:subset] for t in train.tensors))
        if test_subset:
            test = TensorDataset(*(t[:test_subset] for t in test.tensors))
        self.train_data = train
        self.test_data = test
        self.steps_per_epoch = (len(train) + batch_size - 1) // batch_size

        self.model = SmallCNN(width=width)
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=lr)
        self.loss_fn = nn.CrossEntropyLoss()
        self._adapter = TorchTrainerAdapter(self.model, self.optimizer, base_seed=seed)
        self._adapter.extra = {"history": []}

    @staticmethod
    def _load(dataset: str, data_dir: str, seed: int) -> tuple[TensorDataset, TensorDataset]:
        if dataset == "fake":
            return _prepare(*_fake_data(2000, seed)), _prepare(*_fake_data(400, seed + 1))
        if dataset != "cifar10":
            raise ValueError(f"unknown dataset {dataset!r}")
        from torchvision.datasets import CIFAR10

        train = CIFAR10(data_dir, train=True, download=True)
        test = CIFAR10(data_dir, train=False, download=True)
        return _prepare(train.data, train.targets), _prepare(test.data, test.targets)

    def adapter(self) -> TorchTrainerAdapter:
        return self._adapter

    def iter_steps(self, epoch: int, start_step: int) -> Iterator[tuple[int, Any]]:
        self.model.train()
        return self._adapter.iter_batches(
            self.train_data, epoch, start_step, batch_size=self.batch_size
        )

    def train_step(self, batch: Any) -> dict[str, Any]:
        images, labels = batch
        if self.augment:
            flip = torch.rand(images.size(0)) < 0.5
            images = images.clone()
            images[flip] = images[flip].flip(-1)
        self.optimizer.zero_grad()
        loss = self.loss_fn(self.model(images), labels)
        loss.backward()
        self.optimizer.step()
        return {"loss": round(float(loss.item()), 6)}

    @torch.no_grad()
    def evaluate(self) -> tuple[float, float]:
        """Return (loss, accuracy) on the test set."""
        self.model.eval()
        images, labels = self.test_data.tensors
        total_loss = 0.0
        correct = 0
        for start in range(0, len(labels), 512):
            batch_x = images[start : start + 512]
            batch_y = labels[start : start + 512]
            logits = self.model(batch_x)
            total_loss += float(self.loss_fn(logits, batch_y).item()) * len(batch_y)
            correct += int((logits.argmax(1) == batch_y).sum().item())
        self.model.train()
        return total_loss / len(labels), correct / len(labels)

    def end_epoch(self, epoch: int) -> dict[str, Any]:
        loss, accuracy = self.evaluate()
        record = {"epoch": epoch + 1, "test_loss": round(loss, 6), "test_accuracy": accuracy}
        history = self._adapter.extra.setdefault("history", [])
        history[:] = [h for h in history if h["epoch"] != record["epoch"]]
        history.append(record)
        return {"test_loss": record["test_loss"], "test_accuracy": record["test_accuracy"]}

    def result(self) -> dict[str, Any]:
        loss, accuracy = self.evaluate()
        digest = float(sum(p.detach().double().abs().sum().item() for p in self.model.parameters()))
        return {
            "test_loss": loss,
            "test_accuracy": accuracy,
            "weights_abs_sum": digest,
            "history": self._adapter.extra.get("history", []),
        }
