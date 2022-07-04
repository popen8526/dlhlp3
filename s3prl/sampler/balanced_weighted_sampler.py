import torch
from collections import OrderedDict, Counter
from typing import Iterator, TypeVar

from torch.utils.data import Sampler, WeightedRandomSampler

from .base import Sampler

T_co = TypeVar("T_co", covariant=True)


class BalancedWeightedSampler(Sampler):
    def __init__(
        self,
        dataset,
        batch_size: int,
        get_weights: callable = None,
        duplicate: int = 1,
        seed: int = 12345678,
    ) -> None:
        super().__init__(dataset)
        self.epoch = 0
        self.seed = seed
        self.batch_size = batch_size
        self.duplicate = duplicate

        get_weights = get_weights or self.get_weights
        self.weights = get_weights(dataset)

    @staticmethod
    def get_weights(dataset):
        class2weight = Counter()
        weights = []
        with dataset.output_keys_as(["label"]):
            for data_index, item in enumerate(dataset):
                label = item["label"]
                class2weight.update([label])

            for item in dataset:
                weights.append(len(dataset) / class2weight[item["label"]])
        return weights

    def set_epoch(self, epoch: int):
        self.epoch = epoch

    def __iter__(self) -> Iterator[T_co]:
        generator = torch.Generator()
        generator.manual_seed(self.epoch + self.seed)

        sampler = WeightedRandomSampler(self.weights, len(self.weights) * self.duplicate, generator=generator)
        indices = list(sampler)

        batch = []
        for indice in indices:
            batch.append(indice)
            if len(batch) == self.batch_size:
                yield batch
                batch = []

    def __len__(self):
        return len(list(iter(self)))