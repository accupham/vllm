import os
from typing import Dict, List, Optional, Tuple

import torch

from vllm.config import (CacheConfig, DeviceConfig, LoRAConfig, ModelConfig,
                         ParallelConfig, SchedulerConfig)
from vllm.executor.executor_base import ExecutorBase
from vllm.logger import init_logger
from vllm.lora.request import LoRARequest
from vllm.sequence import SamplerOutput, SequenceGroupMetadata
from vllm.utils import get_distributed_init_method, get_ip, get_open_port

logger = init_logger(__name__)


class CPUExecutor(ExecutorBase):

    def __init__(self, model_config: ModelConfig, cache_config: CacheConfig,
                 parallel_config: ParallelConfig,
                 scheduler_config: SchedulerConfig,
                 device_config: DeviceConfig,
                 lora_config: Optional[LoRAConfig], *args, **kwargs) -> None:
        assert device_config.device_type == "cpu"
        assert lora_config is None, "cpu backend doesn't support LoRA"
        model_config = _verify_and_get_model_config(model_config)
        cache_config = _verify_and_get_cache_config(cache_config)
        scheduler_config = _verify_and_get_scheduler_config(scheduler_config)

        self.model_config = model_config
        self.cache_config = cache_config
        self.lora_config = lora_config
        self.parallel_config = parallel_config
        self.scheduler_config = scheduler_config
        self.device_config = device_config

        # Instantiate the worker and load the model to CPU.
        self._init_worker()

    def _init_worker(self):
        from vllm.worker.cpu_worker import CPUWorker

        assert self.parallel_config.world_size == 1, (
            "CPUExecutor only supports single CPU socket currently.")

        distributed_init_method = get_distributed_init_method(
            get_ip(), get_open_port())
        self.driver_worker = CPUWorker(
            model_config=self.model_config,
            parallel_config=self.parallel_config,
            scheduler_config=self.scheduler_config,
            device_config=self.device_config,
            cache_config=self.cache_config,
            local_rank=0,
            rank=0,
            distributed_init_method=distributed_init_method,
            lora_config=self.lora_config,
            kv_cache_dtype=self.cache_config.cache_dtype,
            is_driver_worker=True,
        )
        self.driver_worker.init_device()
        self.driver_worker.load_model()

    def determine_num_available_blocks(self) -> Tuple[int, int]:
        """Determine the number of available KV blocks by invoking the
        underlying worker.
        """
        return self.driver_worker.determine_num_available_blocks()

    def initialize_cache(self, num_gpu_blocks: int,
                         num_cpu_blocks: int) -> None:
        """Initialize the KV cache by invoking the underlying worker.
        """
        # NOTE: We log here to avoid multiple logs when number of workers is
        # greater than one. We could log in the engine, but not all executors
        # have GPUs.
        logger.info(f"# CPU blocks: {num_cpu_blocks}")
        self.driver_worker.initialize_cache(num_gpu_blocks, num_cpu_blocks)

    def execute_model(self,
                      seq_group_metadata_list: List[SequenceGroupMetadata],
                      blocks_to_swap_in: Dict[int, int],
                      blocks_to_swap_out: Dict[int, int],
                      blocks_to_copy: Dict[int, List[int]]) -> SamplerOutput:
        output = self.driver_worker.execute_model(
            seq_group_metadata_list=seq_group_metadata_list,
            blocks_to_swap_in=blocks_to_swap_in,
            blocks_to_swap_out=blocks_to_swap_out,
            blocks_to_copy=blocks_to_copy,
        )
        return output

    def add_lora(self, lora_request: LoRARequest) -> bool:
        return self.driver_worker.add_lora(lora_request)

    def remove_lora(self, lora_id: int) -> bool:
        return self.driver_worker.remove_lora(lora_id)

    def list_loras(self) -> List[int]:
        return self.driver_worker.list_loras()

    def check_health(self) -> None:
        # CPUExecutor will always be healthy as long as
        # it's running.
        return


def _verify_and_get_model_config(config: ModelConfig) -> ModelConfig:
    if config.dtype == torch.float16:
        logger.warning("float16 is not supported on CPU, casting to bfloat16.")
        config.dtype = torch.bfloat16
    if not config.enforce_eager:
        logger.warning(
            "CUDA graph is not supported on CPU, fallback to the eager "
            "mode.")
        config.enforce_eager = True
    return config


def _verify_and_get_scheduler_config(
        config: SchedulerConfig) -> SchedulerConfig:
    if config.chunked_prefill_enabled:
        logger.warning("Chunked prefill is not supported on CPU, disable it.")
        config.chunked_prefill_enabled = False

    return config


def _verify_and_get_cache_config(config: CacheConfig) -> CacheConfig:
    _GB = 1 << 30
    if config.enable_prefix_caching:
        logger.warning("Prefix caching is not supported on CPU, disable it.")
        config.enable_prefix_caching = False

    kv_cache_space_str = os.getenv("VLLM_CPU_KVCACHE_SPACE", "0")
    kv_cache_space = int(kv_cache_space_str)

    if kv_cache_space >= 0:
        if kv_cache_space == 0:
            config.cpu_kvcache_space_bytes = 4 * _GB  # type: ignore
            logger.warning("Environment variable VLLM_CPU_KVCACHE_SPACE (GB) "
                           "for CPU backend is not set, using 4 by default.")
        else:
            config.cpu_kvcache_space_bytes = kv_cache_space * _GB  # type: ignore
    else:
        raise RuntimeError(
            "Invalid environment variable VLLM_CPU_KVCACHE_SPACE"
            f" {kv_cache_space}, expect a positive integer value.")

    return config
