import math
import logging
import argparse
from tqdm import tqdm
from pathlib import Path

import torch
import torch.optim as optim
from torch.utils.data import DataLoader

from s3prl import Object, Output, Logs
from s3prl.superb import asr as problem
from s3prl.sampler import DistributedBatchSamplerWrapper
from s3prl.nn import UpstreamDownstreamModel, S3PRLUpstream

device = "cuda" if torch.cuda.is_available() else "cpu"
logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("librispeech", help="The root directory of LibriSpeech")
    parser.add_argument("save_to", help="The directory to save checkpoint")
    parser.add_argument("--total_steps", type=int, default=200000)
    parser.add_argument("--log_step", type=int, default=100)
    parser.add_argument("--eval_step", type=int, default=5000)
    parser.add_argument("--save_step", type=int, default=100)
    args = parser.parse_args()
    return args


def main():
    logging.basicConfig()
    logger.setLevel(logging.INFO)

    args = parse_args()
    librispeech = Path(args.librispeech)
    assert librispeech.is_dir()
    save_to = Path(args.save_to)
    save_to.mkdir(exist_ok=True, parents=True)

    logger.info("Preparing preprocessor")
    preprocessor = problem.Preprocessor(
        librispeech, splits=["train-clean-100", "dev-clean", "test-clean"]
    )

    logger.info("Preparing train dataloader")
    train_dataset = problem.TrainDataset(**preprocessor.train_data())
    train_sampler = problem.TrainSampler(
        train_dataset, max_timestamp=16000 * 1000, shuffle=True
    )
    train_sampler = DistributedBatchSamplerWrapper(
        train_sampler, num_replicas=1, rank=0
    )
    train_dataloader = DataLoader(
        train_dataset,
        batch_sampler=train_sampler,
        num_workers=4,
        collate_fn=train_dataset.collate_fn,
    )

    logger.info("Preparing valid dataloader")
    valid_dataset = problem.ValidDataset(
        **preprocessor.valid_data(),
        **train_dataset.statistics(),
    )
    valid_dataset.save_checkpoint(save_to / "valid_dataset.ckpt")
    valid_sampler = problem.ValidSampler(valid_dataset, 8)
    valid_sampler = DistributedBatchSamplerWrapper(
        valid_sampler, num_replicas=1, rank=0
    )
    valid_dataloader = DataLoader(
        valid_dataset,
        batch_sampler=valid_sampler,
        num_workers=4,
        collate_fn=valid_dataset.collate_fn,
    )

    logger.info("Preparing test dataloader")
    test_dataset = problem.TestDataset(
        **preprocessor.test_data(),
        **train_dataset.statistics(),
    )
    test_dataset.save_checkpoint(save_to / "test_dataset.ckpt")
    test_sampler = problem.TestSampler(test_dataset, 8)
    test_sampler = DistributedBatchSamplerWrapper(test_sampler, num_replicas=1, rank=0)
    test_dataloader = DataLoader(
        test_dataset,
        batch_sampler=test_sampler,
        num_workers=4,
        collate_fn=test_dataset.collate_fn,
    )

    latest_task = save_to / "task.ckpt"
    if latest_task.is_file():
        logger.info("Last checkpoint found. Load model and optimizer from checkpoint")

        # Object.load_checkpoint() from a checkpoint path and
        # Object.from_checkpoint() from a loaded checkpoint dictionary
        # are like AutoModel in Huggingface which you only need to
        # provide the checkpoint for restoring the module.
        #
        # Note that source code definition should be importable, since this
        # auto loading mechanism is just automating the model re-initialization
        # steps instead of scriptify (torch.jit) all the source code in the
        # checkpoint

        task = Object.load_checkpoint(latest_task).to(device)

    else:
        logger.info("No last checkpoint found. Create new model")

        # Model creation block which can be fully customized
        upstream = S3PRLUpstream("apc")
        downstream = problem.DownstreamModel(
            upstream.output_size,
            preprocessor.statistics().output_size,
            hidden_size=[512],
            dropout=[0.2],
        )
        model = UpstreamDownstreamModel(upstream, downstream)

        # After customize your own model, simply put it into task object
        task = problem.Task(model, preprocessor.statistics().label_loader)
        task = task.to(device)

    # We do not handle optimizer/scheduler in any special way in S3PRL, since
    # there are lots of dedicated package for this. Hence, we also do not handle
    # the checkpointing for optimizer/scheduler. Depends on what training pipeline
    # the user prefer, either Lightning or SpeechBrain, these frameworks will
    # provide different solutions on how to save these objects. By not handling
    # these objects in S3PRL we are making S3PRL more flexible and agnostic to training pipeline
    # The following optimizer codeblock aims to align with the standard usage
    # of PyTorch which is the standard way to save it.

    optimizer = optim.Adam(task.parameters(), lr=1e-3)
    latest_optimizer = save_to / "optimizer.ckpt"
    if latest_optimizer.is_file():
        optimizer.load_state_dict(torch.load(save_to / "optimizer.ckpt"))
    else:
        optimizer = optim.Adam(task.parameters(), lr=1e-3)

    # The following code block demonstrate how to train with your own training loop
    # This entire block can be easily replaced with Lightning/SpeechBrain Trainer as
    #
    #     Trainer(task)
    #     Trainer.fit(train_dataloader, valid_dataloader, test_dataloader)
    #
    # As you can see, there is a huge similarity among train/valid/test loops below,
    # so it is a natural step to share these logics with a generic Trainer class
    # as done in Lightning/SpeechBrain

    pbar = tqdm(total=args.total_steps, desc="Total")
    while True:
        batch_results = []
        for batch in tqdm(train_dataloader, desc="Train", total=len(train_dataloader)):
            pbar.update(1)
            global_step = pbar.n

            assert isinstance(batch, Output)
            optimizer.zero_grad()

            # An Output object can more all its direct
            # attributes/values to the device
            batch = batch.to(device)

            # An Output object is an OrderedDict so we
            # can use dict decomposition here
            task.train()
            result = task.train_step(**batch)
            assert isinstance(result, Output)

            # The output of train step must contain
            # at least a loss key
            result.loss.backward()

            # gradient clipping
            grad_norm = torch.nn.utils.clip_grad_norm_(task.parameters(), max_norm=1.0)

            if math.isnan(grad_norm):
                logger.warning(f"Grad norm is NaN at step {global_step}")
            else:
                optimizer.step()

            # Detach from GPU, remove large logging (to Tensorboard or local files)
            # objects like logits and leave only small data like loss scalars / prediction
            # strings, so that these objects can be safely cached in a list in the MEM,
            # and become useful for calculating metrics later
            # The Output class can do these with self.cacheable()
            cacheable_result = result.cacheable()

            # Cache these small data for later metric calculation
            batch_results.append(cacheable_result)

            if (global_step + 1) % args.log_step == 0:
                logs: Logs = task.train_reduction(batch_results).logs
                logger.info(f"[Train] step {global_step}")
                for log in logs.values():
                    logger.info(f"{log.name}: {log.data}")
                batch_results = []

            if (global_step + 1) % args.eval_step == 0:
                with torch.no_grad():
                    task.eval()

                    # valid
                    valid_results = []
                    for batch in tqdm(
                        valid_dataloader, desc="Valid", total=len(valid_dataloader)
                    ):
                        batch = batch.to(device)
                        result = task.valid_step(**batch)
                        cacheable_result = result.cacheable()
                        valid_results.append(cacheable_result)

                    logs: Logs = task.valid_reduction(valid_results).logs
                    logger.info(f"[Valid] step {global_step}")
                    for log in logs.values():
                        logger.info(f"{log.name}: {log.data}")

            if (global_step + 1) % args.save_step == 0:
                task.save_checkpoint(save_to / "task.ckpt")
                torch.save(optimizer.state_dict(), save_to / "optimizer.ckpt")

    with torch.no_grad():
        # test
        test_results = []
        for batch in tqdm(test_dataloader, desc="Test", total=len(test_dataloader)):
            batch = batch.to(device)
            result = task.test_step(**batch)
            cacheable_result = result.cacheable()
            test_results.append(cacheable_result)

        logs: Logs = task.test_reduction(test_results).logs
        logger.info(f"[Test] step results")
        for log in logs.values():
            logger.info(f"{log.name}: {log.data}")


if __name__ == "__main__":
    main()