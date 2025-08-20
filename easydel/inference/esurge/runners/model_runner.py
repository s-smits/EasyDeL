# Copyright 2025 The EasyDeL Author @erfanzar (Erfan Zare Chavoshi).
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""eSurge Model Runner - High-performance inference execution engine.

This module implements the core execution logic for the eSurge inference engine,
providing efficient model execution with advanced features like paged attention,
dynamic batching, and compilation caching.

Key Components:
    ExecutorManager: Manages compiled execution functions for different batch/token configurations
    eSurgeRunner: Main runner class that orchestrates model execution

Architecture:
    The module uses a two-stage compilation strategy:
    1. Pre-compilation of functions for different token/batch size combinations
    2. Runtime selection of appropriate compiled function based on input shape

Performance Features:
    - Paged attention for efficient KV cache management
    - Vectorized operations for batch processing
    - Pre-allocated buffers to minimize memory allocation
    - Compilation caching to avoid recompilation
    - Progress logging for long compilation processes

Example:
    >>> from easydel.infra import EasyDeLBaseModule
    >>> from easydel.inference.esurge.runners import eSurgeRunner
    >>>
    >>> # Initialize model
    >>> model = EasyDeLBaseModule.from_pretrained("model-name")
    >>>
    >>> # Create runner
    >>> runner = eSurgeRunner(
    ...     model=model,
    ...     max_model_len=2048,
    ...     max_num_seqs=8,
    ...     hbm_utilization=0.9
    ... )
    >>>
    >>> # Compile for different configurations
    >>> runner.compile()
    >>>
    >>> # Execute model
    >>> output = runner.execute_model(scheduler_output)
"""

from __future__ import annotations

import time
import typing
from functools import partial

import jax
from eformer import escale as es
from flax import nnx as nn
from jax import numpy as jnp
from jax._src import pjit

from easydel.layers.caching import PagesCache, PagesCacheMetaData, PagesMetadata
from easydel.utils import ProgressLogger, ejit, get_logger

from ...vsurge.core.functions import sample_top_p_efficient
from ..metrics import get_metrics_collector
from ..outputs import LogprobsTensors, ModelRunnerOutput
from ..page_table import PAGE_TABLE_PADDING_VAL, SLOT_MAPPING_PADDING_VAL
from ..scheduler import SchedulerOutput
from .sequence_buffer import ModelRunnerSamplingMetadata, SequenceBuffer
from .states import CachedRequestState

if typing.TYPE_CHECKING:
    from easydel.infra import EasyDeLBaseModule

logger = get_logger("eSurge")


class ExecutorManager:
    """Manages precompiled execution functions for efficient model inference.

    This class handles the compilation and caching of model execution functions
    for different batch sizes and token counts. It supports two execution modes:
    - Combined forward: Single function for both hidden states and token generation
    - Separate functions: Separate functions for hidden states and token generation

    The manager supports both AOT (Ahead-of-Time) and JIT (Just-In-Time) compilation:
    - AOT mode (default): Pre-compiles functions using JAX's lower/compile API for
      optimal performance in production
    - JIT mode: Compiles functions on first use with graph definition as static
      argument, more flexible for development

    The manager pre-compiles functions for various configurations to avoid
    runtime compilation overhead, enabling seamless switching between different
    batch sizes and sequence lengths.

    Attributes:
        model: The EasyDeL model being managed.
        mesh: JAX sharding mesh for distributed execution.
        kv_pages: KV cache pages for attention.
        use_combined_forward: Whether to use combined or separate functions.
        use_aot_forward: Whether to use AOT compilation (default: True).
        graphdef, graphstate, graphother: Split model components for JAX.
        _lowerd_history: Cache of compiled functions.

    Example:
        >>> executor = ExecutorManager(
        ...     model=my_model,
        ...     mesh=device_mesh,
        ...     kv_pages=cache_pages,
        ...     use_combined_forward=True,
        ...     use_aot_forward=True  # Use AOT compilation
        ... )
        >>> executor.compile(token_paddings, ...)
        >>> tokens = executor.execute(inputs, ...)
    """

    def __init__(
        self,
        model: EasyDeLBaseModule,
        mesh: jax.sharding.Mesh,
        kv_pages: PagesCache,
        use_combined_forward: bool = False,
        use_aot_forward: bool = True,
        min_input_pad: int = 8,
    ):
        """Initialize the executor manager.

        Args:
            model: The EasyDeL model instance.
            mesh: JAX sharding mesh for distributed execution.
            kv_pages: Pages cache for KV cache management.
            use_combined_forward: Whether to use combined forward pass for model and token
                generation in a single function call. Default is False.
            use_aot_forward: Whether to use Ahead-of-Time (AOT) compilation for model
                execution. When True (default), functions are pre-compiled for better
                performance. When False, uses Just-In-Time (JIT) compilation with
                the graph definition passed as a static argument.
        """
        logger.info(f"Initializing ExecutorManager with use_combined_forward={use_combined_forward}")
        self.model = model
        self.mesh = mesh
        self.kv_pages = kv_pages
        self.use_combined_forward = use_combined_forward
        self.use_aot_forward = use_aot_forward
        self.min_input_pad = min_input_pad
        logger.debug("Splitting model module for graph-based execution")
        self.graphdef, self.graphstate, self.graphother = model.split_module()

        self.rng_key = jax.random.PRNGKey(0)

        self._empty_sharding = jax.NamedSharding(mesh, jax.sharding.PartitionSpec())

        self._main_fn: None | pjit.JitWrapped = None
        self._compute_hidden_states_fn: None | pjit.JitWrapped = None
        self._compute_tokens_fn: None | pjit.JitWrapped = None

        self._lowerd_history = dict()

        logger.debug("Initializing execution functions")
        self.init_fns()
        logger.debug("ExecutorManager initialization complete")

    def execute(
        self,
        input_ids_view: jax.Array,
        position_ids_view: jax.Array,
        cache_metadata: PagesMetadata,
        logits_indices: jax.Array,
        sampling_metadata: ModelRunnerSamplingMetadata,
        padded_num_reqs: int,
    ) -> tuple[jax.Array, jax.Array | None]:
        """Execute the model on prepared inputs.

        Selects and runs the appropriate pre-compiled function based on
        input shapes. Handles both combined and separate execution modes.

        When AOT compilation is disabled (use_aot_forward=False), the graph
        definition is passed as a static argument during execution for JIT
        compilation. When enabled (default), pre-compiled functions are used
        for better performance.

        Args:
            input_ids_view: Token IDs to process [num_tokens].
            position_ids_view: Position IDs for tokens [num_tokens].
            cache_metadata: Paged attention metadata.
            logits_indices: Indices for logit extraction.
            sampling_metadata: Parameters for token sampling.
            padded_num_reqs: Padded number of requests.

        Returns:
            tuple: (sampled_token_ids, logits or None)
                - sampled_token_ids: Generated token IDs.
                - logits: Raw logits (only in separate mode).
        """
        static_arguments = (self.graphdef,) if not self.use_aot_forward else ()
        if self.use_combined_forward:
            fn = self.get_compiled_key(input_ids_view.shape[0], padded_num_reqs)
            token_ids, self.kv_pages, self.rng_key = fn(
                *static_arguments,
                self.graphstate,
                self.graphother,
                input_ids_view,
                position_ids_view,
                self.kv_pages,
                cache_metadata,
                logits_indices,
                sampling_metadata,
                self.rng_key,
            )
            return token_ids, None
        else:
            hfn, tfn = self.get_compiled_key(input_ids_view.shape[0], padded_num_reqs)
            hidden_states, self.kv_pages = hfn(
                *static_arguments,
                self.graphstate,
                self.graphother,
                input_ids_view,
                position_ids_view,
                self.kv_pages,
                cache_metadata,
            )
            token_ids, self.rng_key = tfn(
                *static_arguments,
                self.graphstate,
                self.graphother,
                hidden_states,
                logits_indices,
                sampling_metadata,
                self.rng_key,
            )
            return token_ids, token_ids

    def compile(
        self,
        num_tokens_paddings: list[int],
        num_reqs_max_model_len: int,
        max_pages_per_req: int,
        max_num_reqs: int,
        metadata: PagesCacheMetaData,
    ):
        logger.debug(f"Starting compilation for {len(num_tokens_paddings)} token padding sizes")
        logger.debug(f"Token paddings: {num_tokens_paddings}")
        logger.debug(f"Max pages per request: {max_pages_per_req}, Max requests: {max_num_reqs}")

        ufn = partial(_get_padded_num_reqs_with_upper_limit, min_input_pad=self.min_input_pad)
        reqs_padds = list(set([ufn(num_reqs, max_num_reqs) for num_reqs in range(max_num_reqs)]))
        total_compilations = len(num_tokens_paddings) * len(reqs_padds)
        compilation_count = 0

        # Use the new ProgressLogger
        progress = ProgressLogger("eSurge", logger)

        for num_tokens in num_tokens_paddings:
            for reqs_padd in reqs_padds:
                compile_start = time.time()

                # Update progress
                progress_msg = (
                    f"Compiling [{compilation_count + 1}/{total_compilations}]:"
                    f" {num_tokens:5d} tokens, {reqs_padd:2d} padded requests"
                )
                progress.update(compilation_count, total_compilations, progress_msg)

                self._step_compile(
                    num_tokens=num_tokens,
                    num_reqs_max_model_len=num_reqs_max_model_len,
                    max_pages_per_req=max_pages_per_req,
                    max_num_reqs=max_num_reqs,
                    padded_num_reqs=reqs_padd,
                    metadata=metadata,
                )
                compile_time = time.time() - compile_start
                logger.debug(f"Step completed in {compile_time:.2f}s")
                compilation_count += 1

        # Complete the progress
        progress.complete(f"All {total_compilations} compilations completed")

    def _step_compile(
        self,
        num_tokens: int,
        num_reqs_max_model_len: int,
        max_pages_per_req: int,
        max_num_reqs: int,
        padded_num_reqs: int,
        metadata: PagesCacheMetaData,
    ) -> bool:
        """Compile a single step configuration."""
        compargs = self.get_compile_configurations(
            self.kv_pages,
            self.rng_key,
            num_tokens,
            num_reqs_max_model_len,
            max_pages_per_req,
            max_num_reqs,
            padded_num_reqs,
            metadata,
        )
        self.compile_key(num_tokens, padded_num_reqs, compargs)

    def init_fns(self):
        self._main_fn = self.get_fn()
        self._compute_hidden_states_fn = self.get_compute_hidden_states_fn()
        self._compute_tokens_fn = self.get_compute_tokens_fn()

    def get_compute_hidden_states_fn(self) -> typing.Callable:
        @ejit(
            static_argnums=(0,),
            donate_argnames=["input_ids", "position_ids", "kv_pages"],
            in_shardings=(
                es.extract_shardings(self.graphstate, self.mesh),
                es.extract_shardings(self.graphother, self.mesh),
                self._empty_sharding,  # input_ids
                self._empty_sharding,  # position_ids
                es.extract_shardings(self.kv_pages, self.mesh),  # kv_pages
                self._empty_sharding,  # cache_metadata
            ),
            out_shardings=(self._empty_sharding, es.extract_shardings(self.kv_pages, self.mesh)),
        )
        def _fn(
            graphdef,
            graphstate,
            graphother,
            input_ids: jax.Array,
            position_ids: jax.Array,
            kv_pages: PagesCache,
            cache_metadata: PagesMetadata,
        ):
            model: EasyDeLBaseModule = nn.merge(graphdef, graphstate, graphother)
            with model.mesh:
                output = model(
                    input_ids=jnp.expand_dims(input_ids, 0),
                    position_ids=jnp.expand_dims(position_ids, 0),
                    past_key_values=kv_pages,
                    cache_metadata=cache_metadata,
                    apply_lm_head=False,
                )
                return output.last_hidden_state.squeeze(0), output.past_key_values

        return _fn

    def get_compute_tokens_fn(self) -> typing.Callable:
        @ejit(
            static_argnums=(0,),
            in_shardings=(
                es.extract_shardings(self.graphstate, self.mesh),
                es.extract_shardings(self.graphother, self.mesh),
                self._empty_sharding,  # hidden_states
                self._empty_sharding,  # logits_indices
                self._empty_sharding,  # sampling_params
                self._empty_sharding,  # rng_key
            ),
            out_shardings=(self._empty_sharding, self._empty_sharding),
        )
        def _fn(
            graphdef,
            graphstate,
            graphother,
            hidden_states: jax.Array,
            logits_indices: jax.Array,
            sampling_params: ModelRunnerSamplingMetadata,
            rng_key: jax.random.PRNGKey,
        ):
            model: EasyDeLBaseModule = nn.merge(graphdef, graphstate, graphother)
            with model.mesh:
                logits = model.apply_lm_head(hidden_states[logits_indices])
                keys = jax.random.split(rng_key, logits.shape[0] + 1)
                samples = jax.vmap(sample_top_p_efficient, in_axes=(0, 0, 0, 0, None), out_axes=0)(
                    logits,
                    sampling_params.top_p.astype(logits.dtype),
                    sampling_params.temperature.astype(logits.dtype),
                    keys[1:],
                    64,
                )
                return samples.reshape(-1, 1), keys[0]

        return _fn

    def get_fn(self) -> typing.Callable:
        """Precompile the forward pass and token computation function."""

        @ejit(
            static_argnums=(0,),
            donate_argnames=["input_ids", "position_ids", "kv_pages"],
            in_shardings=(
                es.extract_shardings(self.graphstate, self.mesh),
                es.extract_shardings(self.graphother, self.mesh),
                self._empty_sharding,  # input_ids
                self._empty_sharding,  # position_ids
                es.extract_shardings(self.kv_pages, self.mesh),  # kv_pages
                self._empty_sharding,  # cache_metadata
                self._empty_sharding,  # logits_indices
                self._empty_sharding,  # sampling_params
                self._empty_sharding,  # rng_key
            ),
            out_shardings=(
                self._empty_sharding,
                es.extract_shardings(self.kv_pages, self.mesh),
                self._empty_sharding,
            ),
        )
        def _fn(
            graphdef,
            graphstate,
            graphother,
            input_ids: jax.Array,
            position_ids: jax.Array,
            kv_pages: PagesCache,
            cache_metadata: PagesMetadata,
            logits_indices: jax.Array,
            sampling_params: ModelRunnerSamplingMetadata,
            rng_key: jax.random.PRNGKey,
        ):
            model: EasyDeLBaseModule = nn.merge(graphdef, graphstate, graphother)
            with model.mesh:
                output = model(
                    input_ids=jnp.expand_dims(input_ids, 0),
                    position_ids=jnp.expand_dims(position_ids, 0),
                    past_key_values=kv_pages,
                    cache_metadata=cache_metadata,
                    apply_lm_head=False,
                )
                logits = model.apply_lm_head(output.last_hidden_state.squeeze(0)[logits_indices])
                keys = jax.random.split(rng_key, logits.shape[0] + 1)

                samples = jax.vmap(
                    sample_top_p_efficient,
                    in_axes=(0, 0, 0, 0, None),
                    out_axes=0,
                )(
                    logits,
                    sampling_params.top_p.astype(logits.dtype),
                    sampling_params.temperature.astype(logits.dtype),
                    keys[1:],
                    32,
                )
                return samples.reshape(-1, 1), output.past_key_values, keys[0]

        return _fn

    def compile_key(self, num_tokens: int, padded_num_reqs: int, compargs):
        """Compile model execution functions for specific input dimensions.

        Handles both AOT and JIT compilation modes based on use_aot_forward flag.
        For AOT mode (default), pre-compiles functions using JAX's lower/compile API.
        For JIT mode, executes functions once to trigger JIT compilation and caches
        the wrapped functions.

        Args:
            num_tokens: Number of tokens in the input batch.
            padded_num_reqs: Padded number of requests for batching.
            compargs: Compilation arguments for the model functions.
        """
        if self.use_aot_forward:
            if self.use_combined_forward:
                logger.debug(f"Compiling combined forward function for key ({num_tokens}, {padded_num_reqs})")
                lowered = self._main_fn.lower(*compargs)
                compiled = lowered.compile()
                self._lowerd_history[(num_tokens, padded_num_reqs)] = compiled
            else:
                hskey = (num_tokens, padded_num_reqs, "hidden_states")
                tskey = (num_tokens, padded_num_reqs, "tokens")
                if hskey not in self._lowerd_history.keys():
                    logger.debug(f"Compiling hidden states function for key {hskey}")
                    hidden_states_lowered = self._compute_hidden_states_fn.lower(*compargs[0])
                    hidden_states_compiled = hidden_states_lowered.compile()
                    self._lowerd_history[hskey] = hidden_states_compiled
                if tskey not in self._lowerd_history.keys():
                    logger.debug(f"Compiling tokens function for key {tskey}")
                    tokens_lowered = self._compute_tokens_fn.lower(*compargs[1])
                    tokens_compiled = tokens_lowered.compile()
                    self._lowerd_history[tskey] = tokens_compiled
        else:
            if self.use_combined_forward:
                logger.debug(f"Compiling combined forward function for key ({num_tokens}, {padded_num_reqs})")
                _, self.kv_pages, _ = self._main_fn(*compargs)
                self._lowerd_history[(num_tokens, padded_num_reqs)] = self._main_fn
            else:
                hskey = (num_tokens, padded_num_reqs, "hidden_states")
                tskey = (num_tokens, padded_num_reqs, "tokens")
                if hskey not in self._lowerd_history.keys():
                    logger.debug(f"Compiling hidden states function for key {hskey}")
                    _, self.kv_pages = self._compute_hidden_states_fn(*compargs[0])
                    self._lowerd_history[hskey] = self._compute_hidden_states_fn
                if tskey not in self._lowerd_history.keys():
                    logger.debug(f"Compiling tokens function for key {tskey}")
                    _ = self._compute_tokens_fn(*compargs[1])
                    self._lowerd_history[tskey] = self._compute_tokens_fn

    def get_compiled_key(self, num_tokens: int, padded_num_reqs: int):
        """Retrieve pre-compiled functions for given input dimensions.

        Args:
            num_tokens: Number of tokens in the input batch.
            padded_num_reqs: Padded number of requests for batching.

        Returns:
            Compiled function(s) for the specified dimensions. Returns a single
            function for combined forward mode, or a tuple of (hidden_states_fn,
            tokens_fn) for separate mode.
        """
        if self.use_combined_forward:
            return self._lowerd_history[(num_tokens, padded_num_reqs)]
        else:
            hskey = (num_tokens, padded_num_reqs, "hidden_states")
            tskey = (num_tokens, padded_num_reqs, "tokens")
            return self._lowerd_history[hskey], self._lowerd_history[tskey]

    def get_compile_configurations(
        self,
        kv_pages: PagesCache,
        rng_key: jax.random.PRNGKey,
        num_tokens: int,
        num_reqs_max_model_len: int,
        max_pages_per_req: int,
        max_num_reqs: int,
        padded_num_reqs: int,
        metadata: PagesCacheMetaData,
    ):
        """Returns Compile specific configurations for a function.

        Args:
            func_name: Name of the function to compile
            kv_pages: KV kv_pages pages
            rng_key: Random key for sampling
            num_reqs_max_model_len: Number of requests for max model length
            max_pages_per_req: Maximum pages per request
            max_num_reqs: Maximum number of requests
            metadata: Pages metadata
        """
        actual_num_reqs = min(num_tokens, num_reqs_max_model_len)
        padded_num_slices = metadata.get_padded_num_slices(num_tokens, max_num_reqs)
        query_lens = [1] * num_reqs_max_model_len
        if self.use_combined_forward:
            example_args = (
                self.graphdef,
                self.graphstate,
                self.graphother,
                jnp.zeros((num_tokens,), dtype=jnp.int32),
                jnp.zeros(num_tokens, dtype=jnp.int32),
                kv_pages,
                PagesMetadata(
                    pages_tables=jnp.full(
                        (num_reqs_max_model_len, max_pages_per_req), fill_value=PAGE_TABLE_PADDING_VAL, dtype=jnp.int32
                    ),
                    context_lens=jnp.ones((num_reqs_max_model_len,), dtype=jnp.int32),
                    query_start_loc=jnp.cumsum(jnp.array([0, *query_lens], dtype=jnp.int32), axis=0, dtype=jnp.int32),
                    num_seqs=jnp.array([actual_num_reqs], dtype=jnp.int32),
                    slot_mapping=jnp.full((3, padded_num_slices), fill_value=SLOT_MAPPING_PADDING_VAL, dtype=jnp.int32),
                    num_kv_update_slices=jnp.array([padded_num_slices], dtype=jnp.int32),
                    num_slices_per_kv_cache_update_page=metadata.num_slices_per_kv_cache_update_page,
                    page_size=metadata.page_size,
                ),
                jnp.arange(padded_num_reqs, dtype=jnp.int32),
                ModelRunnerSamplingMetadata(
                    top_p=jnp.ones((padded_num_reqs,), dtype=jnp.float32),
                    temperature=jnp.ones((padded_num_reqs,), dtype=jnp.float32),
                    min_p=jnp.zeros((padded_num_reqs,), dtype=jnp.float32),
                    top_k=jnp.zeros((padded_num_reqs,), dtype=jnp.int32),
                ),
                rng_key,
            )
        else:
            example_args = (
                (
                    self.graphdef,
                    self.graphstate,
                    self.graphother,
                    jnp.zeros((num_tokens,), dtype=jnp.int32),
                    jnp.zeros(num_tokens, dtype=jnp.int32),
                    kv_pages,
                    PagesMetadata(
                        pages_tables=jnp.full(
                            (num_reqs_max_model_len, max_pages_per_req),
                            fill_value=PAGE_TABLE_PADDING_VAL,
                            dtype=jnp.int32,
                        ),
                        context_lens=jnp.ones((num_reqs_max_model_len,), dtype=jnp.int32),
                        query_start_loc=jnp.cumsum(
                            jnp.array([0, *query_lens], dtype=jnp.int32), axis=0, dtype=jnp.int32
                        ),
                        num_seqs=jnp.array([actual_num_reqs], dtype=jnp.int32),
                        slot_mapping=jnp.full(
                            (3, padded_num_slices), fill_value=SLOT_MAPPING_PADDING_VAL, dtype=jnp.int32
                        ),
                        num_kv_update_slices=jnp.array([padded_num_slices], dtype=jnp.int32),
                        num_slices_per_kv_cache_update_page=metadata.num_slices_per_kv_cache_update_page,
                        page_size=metadata.page_size,
                    ),
                ),
                (
                    self.graphdef,
                    self.graphstate,
                    self.graphother,
                    jnp.ones((num_tokens, self.model.config.get_text_config().hidden_size), self.model.dtype),
                    jnp.arange(padded_num_reqs, dtype=jnp.int32),
                    ModelRunnerSamplingMetadata(
                        top_p=jnp.ones((padded_num_reqs,), dtype=jnp.float32),
                        temperature=jnp.ones((padded_num_reqs,), dtype=jnp.float32),
                        min_p=jnp.zeros((padded_num_reqs,), dtype=jnp.float32),
                        top_k=jnp.zeros((padded_num_reqs,), dtype=jnp.int32),
                    ),
                    rng_key,
                ),
            )
        return example_args


def _get_padded_num_reqs_with_upper_limit(x: int, upper_limit: int, min_input_pad: int) -> int:
    """Calculate padded request count for compilation efficiency.

    Pads the number of requests to powers of 2 (up to 8) or the nearest
    power of 2 above 8. This reduces the number of unique compilations
    needed while maintaining good utilization.

    Args:
        x: Actual number of requests
        upper_limit: Maximum allowed requests

    Returns:
        int: Padded request count, capped at upper_limit

    Example:
        >>> _get_padded_num_reqs_with_upper_limit(3, 32)   # Returns 8
        >>> _get_padded_num_reqs_with_upper_limit(10, 32)  # Returns 16
        >>> _get_padded_num_reqs_with_upper_limit(20, 16)  # Returns 16
    """
    res = min_input_pad if x <= min_input_pad else 1 << (x - 1).bit_length()
    return min(res, upper_limit)


class eSurgeRunner:
    """High-performance model runner for efficient batched inference.

    The eSurgeRunner orchestrates model execution with advanced features:
    - Paged attention for memory-efficient KV cache management
    - Dynamic batching with request scheduling
    - Pre-allocated buffers for zero-copy operations
    - Vectorized token processing
    - Compilation caching for different batch/sequence configurations

    The runner maintains an internal state of active requests and manages
    their lifecycle from prompt processing through token generation.

    Architecture:
        Request Flow:
        1. Scheduler provides requests to execute
        2. Runner updates internal state (add/remove requests)
        3. Prepares inputs with proper padding and batching
        4. Executes model using pre-compiled functions
        5. Processes sampled tokens and updates buffers
        6. Returns results to scheduler

    Memory Management:
        - Pre-allocated buffers for common operations
        - Paged KV cache with configurable page size
        - Efficient slot mapping for attention
        - Buffer reuse across batches

    Attributes:
        model: The EasyDeL model to run
        metadata: Paged attention metadata
        max_num_seqs: Maximum concurrent sequences
        max_model_len: Maximum sequence length
        executor_manager: Manages compiled functions
        sequence_buffer: Manages active sequences
        requests: Active request states

    Example:
        >>> runner = eSurgeRunner(
        ...     model=model,
        ...     max_model_len=2048,
        ...     max_num_seqs=8,
        ...     hbm_utilization=0.9,
        ...     page_size=128
        ... )
        >>>
        >>> # Compile for all configurations
        >>> runner.compile()
        >>>
        >>> # Execute requests from scheduler
        >>> output = runner.execute_model(scheduler_output)
        >>>
        >>> # Process results
        >>> for req_id, tokens in zip(output.req_ids, output.sampled_token_ids):
        ...     print(f"Request {req_id}: {tokens}")
    """

    def __init__(
        self,
        model: EasyDeLBaseModule,
        hbm_utilization: float = 0.5,
        page_size: int = 128,
        max_model_len: int = 2**13,
        min_input_pad: int = 256,
        max_num_seqs: int = 16,
        use_combined_forward: bool = False,
        use_aot_forward: bool = True,
        verbose: bool = False,
    ):
        """Initialize the model runner.

        Args:
            model: The EasyDeL model to run inference on
            hbm_utilization: Fraction of HBM to use for KV kv_pages
            page_size: Size of each page in the paged attention mechanism
            max_model_len: Maximum model sequence length
            max_num_seqs: Maximum number of sequences to process in parallel
        """
        logger.debug(f"Initializing eSurgeRunner with {max_model_len=}, {max_num_seqs=}")
        logger.debug(f"Configuration: {hbm_utilization=}, {page_size=}, {use_combined_forward=}, {use_aot_forward=}")
        self.model = model
        self.metadata = model.create_paged_metadata(
            hbm_utilization=hbm_utilization,
            page_size=page_size,
            max_model_length=max_model_len,
        )
        self.max_num_seqs = max_num_seqs
        self.max_num_reqs = max_num_seqs

        self.max_model_len = max_model_len
        self.min_input_pad = min(min_input_pad, max_num_seqs)

        self.page_size = int(self.metadata.page_size)
        self.max_pages_per_req = int(self.metadata.max_num_pages_per_req)
        logger.debug(f"Metadata created: page_size={self.page_size}, max_pages_per_req={self.max_pages_per_req}")

        logger.debug("Creating ExecutorManager and initializing pages cache")
        self.executor_manager = ExecutorManager(
            model=model,
            mesh=model.mesh,
            kv_pages=model.init_pages(self.metadata),
            use_combined_forward=use_combined_forward,
            use_aot_forward=use_aot_forward,
            min_input_pad=self.min_input_pad,
        )
        self.log_it = logger.info if verbose else logger.debug
        logger.debug("Setting up internal variables and buffers")
        self._setup_variables()
        logger.debug("eSurgeRunner initialization complete")

    @property
    def mesh(self):
        """Get the device mesh."""
        return self.model.mesh

    @property
    def _empty_sharding(self):
        """Get empty sharding specification."""
        return jax.NamedSharding(self.mesh, jax.sharding.PartitionSpec())

    def get_prepare_inputs_fn(self):
        """Fully-jitted input prep with fixed shapes and mask-based gathers."""
        paddings = self.num_tokens_paddings_arr
        max_num_reqs = int(self.max_num_reqs)
        page_size = int(self.metadata.page_size)
        max_pages_per_req = int(self.metadata.max_num_pages_per_req)
        num_reqs_max_model_len = int(self.num_reqs_max_model_len)
        slices_per_page = int(self.metadata.num_slices_per_kv_cache_update_page)
        page_table_pad = jnp.int32(PAGE_TABLE_PADDING_VAL)
        slot_mapping_pad = jnp.int32(SLOT_MAPPING_PADDING_VAL)
        max_num_tokens = int(self.max_num_tokens)
        max_padded_slices = int(self.max_padded_slices)

        i_tokens = jnp.arange(max_num_tokens, dtype=jnp.int32)
        i_reqs = jnp.arange(max_num_reqs, dtype=jnp.int32)
        i_rows_pt = jnp.arange(num_reqs_max_model_len, dtype=jnp.int32)
        i_slices = jnp.arange(max_padded_slices, dtype=jnp.int32)

        @ejit(
            static_argnums=(),  # all dynamic, shapes fixed
            donate_argnames=[
                "input_ids_buf",
                "position_ids_buf",
                "query_start_loc_buf",
                "seq_lens_buf",
                "pages_tables_buf",
                "slot_mapping_buf",
            ],
            in_shardings=(
                self._empty_sharding,  # scheduled_full [max_num_reqs]
                self._empty_sharding,  # num_reqs (scalar)
                self._empty_sharding,  # num_computed_tokens [max_num_reqs]
                self._empty_sharding,  # token_ids [max_num_reqs, max_model_len]
                self._empty_sharding,  # seq_page_table [max_num_reqs, max_pages_per_req]
                self._empty_sharding,  # input_ids_buf [max_num_tokens]
                self._empty_sharding,  # position_ids_buf [max_num_tokens]
                self._empty_sharding,  # query_start_loc_buf [max_num_reqs+1]
                self._empty_sharding,  # seq_lens_buf [max_num_reqs]
                self._empty_sharding,  # pages_tables_buf [num_reqs_max_model_len, max_pages_per_req]
                self._empty_sharding,  # slot_mapping_buf [3, max_padded_slices]
            ),
            out_shardings=(
                self._empty_sharding,  # input_ids_buf
                self._empty_sharding,  # position_ids_buf
                self._empty_sharding,  # query_start_loc_buf
                self._empty_sharding,  # seq_lens_buf
                self._empty_sharding,  # pages_tables_buf
                self._empty_sharding,  # slot_mapping_buf
                self._empty_sharding,  # logits_indices_full [max_num_reqs]
                self._empty_sharding,  # padded_total (scalar)
                self._empty_sharding,  # padded_num_reqs (scalar)
                self._empty_sharding,  # num_kv_update_slices (scalar)
                self._empty_sharding,  # padded_num_slices (scalar)
            ),
        )
        def _fn(
            scheduled_full: jax.Array,  # [max_num_reqs] scheduled tokens per req
            num_reqs: jax.Array,  # scalar int32
            num_computed_tokens: jax.Array,  # [max_num_reqs]
            token_ids: jax.Array,  # [max_num_reqs, max_model_len]
            seq_page_table: jax.Array,  # [max_num_reqs, max_pages_per_req]
            input_ids_buf: jax.Array,  # [max_num_tokens]
            position_ids_buf: jax.Array,  # [max_num_tokens]
            query_start_loc_buf: jax.Array,  # [max_num_reqs+1]
            seq_lens_buf: jax.Array,  # [max_num_reqs]
            pages_tables_buf: jax.Array,  # [num_reqs_max_model_len, max_pages_per_req]
            slot_mapping_buf: jax.Array,  # [3, max_padded_slices]
        ):
            nr = jnp.int32(num_reqs)

            # Mask scheduled beyond active nr
            mask_reqs = i_reqs < nr
            scheduled = jnp.where(mask_reqs, scheduled_full, 0)

            # Cum tokens per request and total
            cum = jnp.cumsum(scheduled)  # [max_num_reqs], non-decreasing
            total = jnp.sum(scheduled)  # scalar int32

            # padded_total via searchsorted on paddings
            idx_pad = jnp.searchsorted(paddings, total, side="left")
            idx_pad = jnp.minimum(idx_pad, paddings.shape[0] - 1)
            padded_total = paddings[idx_pad]

            # Fill token-level mapping for all max_num_tokens positions (masked)
            valid_tok = i_tokens < total
            # find req id for each token index: rightmost cum > t
            req_for_tok = jnp.searchsorted(cum, i_tokens, side="right")  # [max_num_tokens], up to max_num_reqs
            req_for_tok = jnp.where(valid_tok, req_for_tok, 0)  # safe index for invalid
            # previous cum
            cum_prev = jnp.concatenate([jnp.zeros((1,), jnp.int32), cum[:-1]])
            base_pos = num_computed_tokens[req_for_tok]
            off_in_req = i_tokens - cum_prev[req_for_tok]
            positions_full = jnp.where(valid_tok, base_pos + off_in_req, 0)

            # Gather input ids; mask invalid tokens to 0
            safe_pos = jnp.where(valid_tok, positions_full, 0)
            in_ids_full = token_ids[req_for_tok, safe_pos]
            in_ids_full = jnp.where(valid_tok, in_ids_full, 0)

            # Write full buffers (we will slice to [:padded_total] on host)
            input_ids_buf = input_ids_buf.at[:].set(in_ids_full)
            position_ids_buf = position_ids_buf.at[:].set(positions_full)

            # query_start_loc and seq_lens (fixed-size)
            qsl = jnp.zeros((max_num_reqs + 1,), dtype=jnp.int32).at[1:].set(cum)
            seq_lens = jnp.where(mask_reqs, num_computed_tokens + scheduled, 0)

            # pages_tables: pad then copy rows under mask (no dynamic slicing)
            pt_src = seq_page_table[:num_reqs_max_model_len, :]
            mask_rows = i_rows_pt < jnp.minimum(nr, jnp.int32(num_reqs_max_model_len))
            pt = jnp.where(mask_rows[:, None], pt_src, page_table_pad)

            # Slot mapping (page-level) without dynamic-length arrays
            s = num_computed_tokens  # [max_num_reqs]
            e = s + scheduled
            lps = s // page_size
            lpe = (jnp.maximum(e, 1) - 1) // page_size
            page_lens = jnp.where(scheduled > 0, lpe - lps + 1, 0)  # [max_num_reqs]
            page_cum = jnp.cumsum(page_lens)
            total_pages = jnp.sum(page_lens)

            # Compute padded_num_slices (upper bound, then capped)
            pages_est = jnp.minimum(2 * jnp.int32(max_num_reqs) + padded_total // page_size, padded_total)
            tmp = (pages_est + jnp.int32(slices_per_page) - 1) // jnp.int32(slices_per_page)
            padded_num_slices = tmp * jnp.int32(slices_per_page)
            padded_num_slices = jnp.minimum(padded_num_slices, jnp.int32(max_padded_slices))

            # For each potential slice index (0..max_padded_slices-1), find (req_id, local_page_offset)
            valid_slice = i_slices < total_pages
            within_pad = i_slices < padded_num_slices
            slice_active = valid_slice & within_pad

            page_cum_prev = jnp.concatenate([jnp.zeros((1,), jnp.int32), page_cum[:-1]])
            req_for_slice = jnp.searchsorted(page_cum, i_slices, side="right")
            req_for_slice = jnp.where(slice_active, req_for_slice, 0)
            local_off = i_slices - page_cum_prev[req_for_slice]

            # Flatten page table for gather
            pt_full = seq_page_table.reshape((-1,))
            # Compute per-slice global page index
            gpi = req_for_slice * jnp.int32(max_pages_per_req) + lps[req_for_slice] + local_off
            page_numbers = jnp.where(slice_active, pt_full[gpi], 0)

            s_mod = s % page_size
            e_mod = ((jnp.maximum(e, 1) - 1) % page_size) + 1
            lens_rep = page_lens[req_for_slice]

            is_first = local_off == 0
            is_last = local_off == (lens_rep - 1)

            kv_local_st = jnp.where(is_first, s_mod[req_for_slice], jnp.int32(0))
            kv_local_en = jnp.where(is_last, e_mod[req_for_slice], jnp.int32(page_size))
            slice_lens = jnp.maximum(kv_local_en - kv_local_st, 0)
            kv_cache_start = kv_local_st + page_numbers * page_size

            # cumulative new_kv_start across valid slices
            slice_lens_masked = jnp.where(slice_active, slice_lens, 0)
            csl = jnp.cumsum(slice_lens_masked)
            new_kv_start = jnp.roll(csl, 1).at[0].set(0)
            new_kv_start = jnp.where(slice_active, new_kv_start, 0)

            # Build slot_mapping_buf = [3, max_padded_slices], pad elsewhere
            sm0 = jnp.where(within_pad, jnp.where(slice_active, kv_cache_start, slot_mapping_pad), slot_mapping_pad)
            sm1 = jnp.where(within_pad, jnp.where(slice_active, new_kv_start, slot_mapping_pad), slot_mapping_pad)
            sm2 = jnp.where(within_pad, jnp.where(slice_active, slice_lens, slot_mapping_pad), slot_mapping_pad)
            slot_mapping_buf = slot_mapping_buf.at[0, :].set(sm0)
            slot_mapping_buf = slot_mapping_buf.at[1, :].set(sm1)
            slot_mapping_buf = slot_mapping_buf.at[2, :].set(sm2)

            nr_safe = jnp.maximum(nr, 1)
            next_pow2 = jnp.left_shift(1, jnp.ceil(jnp.log2(nr_safe)).astype(jnp.int32))
            padded_num_reqs = jnp.where(nr <= self.min_input_pad, jnp.int32(self.min_input_pad), next_pow2)
            padded_num_reqs = jnp.minimum(padded_num_reqs, jnp.int32(max_num_reqs))

            tmp_logits = qsl[1:] - 1
            mask_logits = i_reqs < padded_num_reqs
            logits_indices_full = jnp.where(mask_logits, tmp_logits, 0)

            return (
                input_ids_buf.at[:].set(in_ids_full),
                position_ids_buf.at[:].set(positions_full),
                qsl,
                seq_lens,
                pt,
                slot_mapping_buf,
                logits_indices_full,
                padded_total,
                padded_num_reqs,
                total_pages,  # num_kv_update_slices
                padded_num_slices,
            )

        return _fn

    @staticmethod
    def _get_token_paddings(min_token_size: int, max_token_size: int, padding_gap: int) -> list[int]:
        """Generate padding sizes for efficient compilation.

        Args:
            min_token_size: Minimum token size (must be power of 2)
            max_token_size: Maximum token size to cover
            padding_gap: Gap between padding sizes (0 for exponential growth)

        Returns:
            List of padding sizes
        """
        if not ((min_token_size & (min_token_size - 1) == 0) and min_token_size > 0):
            logger.error(f"Invalid min_token_size={min_token_size}, must be power of 2")
            raise ValueError(f"min_token_size must be a power of 2, got {min_token_size}")
        assert (min_token_size & (min_token_size - 1) == 0) and min_token_size > 0
        paddings = []
        num = min_token_size

        if padding_gap == 0:
            while num <= max_token_size:
                paddings.append(num)
                num *= 2
        else:
            while num <= padding_gap:
                paddings.append(num)
                num *= 2
            num //= 2
            while num < max_token_size:
                num += padding_gap
                paddings.append(num)

        return paddings

    def _setup_variables(self):
        """Initialize internal variables and preallocate reusable buffers."""
        self.num_reqs_max_model_len = min(self.metadata.get_max_num_seqs(), self.max_num_reqs)
        self.num_reqs_most_model_len = self.num_reqs_max_model_len
        self.num_tokens_paddings = self._get_token_paddings(
            min_token_size=16,
            max_token_size=self.max_model_len,
            padding_gap=0,
        )
        self.max_num_tokens = self.num_tokens_paddings[-1]
        self.requests: dict[str, CachedRequestState] = {}
        logger.debug(f"Token padding sizes: {len(self.num_tokens_paddings)} levels, max={self.max_num_tokens}")

        logger.debug(
            f"Creating sequence buffer for max_num_reqs={self.max_num_reqs}, max_model_len={self.max_model_len}"
        )
        self.sequence_buffer = SequenceBuffer(
            self.max_num_reqs,
            self.max_model_len,
            self.max_num_tokens,
            self.model.config.get_text_config().vocab_size,
            [self.metadata.page_size],
        )

        self.arange = jnp.arange(self.max_num_tokens, dtype=jnp.int32)
        self.arange_np = jnp.arange(self.max_num_reqs, dtype=jnp.int32)  # Pre-allocate for reuse

        self.input_ids_buf = jnp.zeros((self.max_num_tokens,), dtype=jnp.int32)
        self.position_ids_buf = jnp.zeros((self.max_num_tokens,), dtype=jnp.int32)
        self.query_start_loc_buf = jnp.zeros((self.max_num_reqs + 1,), dtype=jnp.int32)
        self.seq_lens_buf = jnp.zeros((self.max_num_reqs,), dtype=jnp.int32)

        self.pages_tables_buf = jnp.full(
            (self.num_reqs_max_model_len, self.max_pages_per_req),
            fill_value=PAGE_TABLE_PADDING_VAL,
            dtype=jnp.int32,
        )

        self.max_padded_slices = int(self.metadata.get_padded_num_slices(self.max_num_tokens, self.max_num_reqs))
        self.slot_mapping_buf = jnp.full(
            (3, self.max_padded_slices),
            fill_value=SLOT_MAPPING_PADDING_VAL,
            dtype=jnp.int32,
        )

        # Pre-allocate flattened page table buffer to avoid repeated flatten operations
        self.page_table_flat_buf = jnp.zeros(
            (self.num_reqs_max_model_len * self.max_pages_per_req,),
            dtype=jnp.int32,
        )

        # Pre-allocate buffers for slot mapping computation
        self.slot_mapping_scratch_buf = jnp.zeros((self.max_padded_slices, 3), dtype=jnp.int32)
        self.num_tokens_paddings_arr = jnp.array(self.num_tokens_paddings, dtype=jnp.int32)
        self._prepare_inputs_fn = self.get_prepare_inputs_fn()
        logger.debug(f"Allocated buffers: max_padded_slices={self.max_padded_slices}")

    def compile(self):
        """Compile the model for all token padding sizes."""
        logger.info("Starting eSurgeRunner compilation")
        logger.debug(
            f"Compiling for {len(self.num_tokens_paddings)} token padding sizes: {self.num_tokens_paddings[:5]}..."
            if len(self.num_tokens_paddings) > 5
            else f"Compiling for token padding sizes: {self.num_tokens_paddings}"
        )

        self.executor_manager.compile(
            num_tokens_paddings=self.num_tokens_paddings,
            num_reqs_max_model_len=self.num_reqs_max_model_len,
            max_pages_per_req=self.max_pages_per_req,
            max_num_reqs=self.max_num_reqs,
            metadata=self.metadata,
        )

    @staticmethod
    def _vectorized_slot_mapping(
        num_computed_tokens: jax.Array,  # [>=num_reqs]
        num_scheduled_tokens_per_req: jax.Array,  # [num_reqs]
        page_table_flat: jax.Array,  # [num_reqs * max_num_pages_per_req]
        num_reqs: int,
        page_size: int,
        max_num_pages_per_req: int,
    ) -> jax.Array:
        """Compute slot mapping for paged attention in vectorized manner.

        Creates a mapping from new KV values to their storage locations
        in the paged KV cache. This is critical for efficient attention
        computation with paged memory management.

        The output array has shape [total_slices, 3] where each row contains:
        - kv_cache_start: Starting position in KV cache
        - new_kv_start: Starting position in new KV values
        - slice_len: Length of this slice

        Args:
            num_computed_tokens: Tokens already processed per request
            num_scheduled_tokens_per_req: New tokens to process per request
            page_table_flat: Flattened page table for all requests
            num_reqs: Number of active requests
            page_size: Size of each KV cache page
            max_num_pages_per_req: Maximum pages per request

        Returns:
            jax.Array: Slot mapping array [total_slices, 3]

        Note:
            This function is performance-critical and fully vectorized
            to avoid Python loops during execution.
        """
        s = num_computed_tokens[:num_reqs]
        e = s + num_scheduled_tokens_per_req
        lps = s // page_size
        lpe = (e - 1) // page_size
        page_lens = lpe - lps + 1
        total_pages = int(jnp.sum(page_lens))
        if total_pages == 0:
            return jnp.zeros((0, 3), dtype=jnp.int32)

        req_ids = jnp.repeat(jnp.arange(num_reqs, dtype=jnp.int32), page_lens)
        cum_pages = jnp.cumsum(page_lens)
        starts = cum_pages - page_lens
        local_page_offsets = jnp.arange(total_pages, dtype=jnp.int32) - jnp.repeat(starts, page_lens)
        local_starts_rep = lps[req_ids]
        global_page_indices = req_ids * max_num_pages_per_req + local_starts_rep + local_page_offsets
        page_numbers = page_table_flat[global_page_indices]
        s_mod = s % page_size
        e_mod = ((e - 1) % page_size) + 1
        s_mod_rep = s_mod[req_ids]
        e_mod_rep = e_mod[req_ids]
        lens_rep = page_lens[req_ids]

        is_first = local_page_offsets == 0
        is_last = local_page_offsets == (lens_rep - 1)

        kv_local_st = jnp.where(is_first, s_mod_rep, jnp.int32(0))
        kv_local_en = jnp.where(is_last, e_mod_rep, jnp.int32(page_size))
        slice_lens = kv_local_en - kv_local_st
        kv_cache_start = kv_local_st + page_numbers * page_size
        new_kv_start = jnp.cumsum(jnp.pad(slice_lens[:-1], (1, 0)), dtype=jnp.int32)

        return jnp.stack([kv_cache_start, new_kv_start, slice_lens], axis=1)

    def _get_slot_mapping_metadata(self, num_reqs: int, num_scheduled_tokens_per_req: jax.Array) -> jax.Array:
        """Compute metadata mapping slices to KV pages. Returns [total_slices, 3]."""
        if num_reqs == 1 and int(num_scheduled_tokens_per_req[0]) == 1:
            num_computed = int(self.sequence_buffer.num_computed_tokens[0])
            page_idx = num_computed // self.metadata.page_size
            page_offset = num_computed % self.metadata.page_size
            page_num = int(self.sequence_buffer.page_table[0].get_array()[0, page_idx])
            kv_cache_start = page_num * self.metadata.page_size + page_offset
            result = jnp.array([[kv_cache_start, 0, 1]], dtype=jnp.int32)
            return result

        seq_page_table = self.sequence_buffer.page_table[0].get_array()
        page_table_size = num_reqs * self.max_pages_per_req
        self.page_table_flat_buf = self.page_table_flat_buf.at[:page_table_size].set(
            seq_page_table[:num_reqs].reshape(-1)[:page_table_size]
        )
        page_table_flat = self.page_table_flat_buf[:page_table_size]
        result = self._vectorized_slot_mapping(
            self.sequence_buffer.num_computed_tokens,
            num_scheduled_tokens_per_req,
            page_table_flat,
            num_reqs,
            self.metadata.page_size,
            self.metadata.max_num_pages_per_req,
        )
        return result

    def _update_states(self, scheduler_output: SchedulerOutput) -> bool:
        """Update internal states based on scheduler output.

        Synchronizes the runner's internal state with the scheduler's decisions.
        Handles request lifecycle: adding new requests, removing finished ones,
        updating cached requests, and managing the sequence buffer.

        State Updates:
        1. Remove finished requests from tracking
        2. Remove unscheduled requests from buffer
        3. Add new requests with their metadata
        4. Update cached request states
        5. Reorganize sequence buffer for efficiency

        Args:
            scheduler_output: Contains request scheduling decisions

        Returns:
            bool: True if state changed (requests added/removed),
                  indicating potential buffer reorganization

        Side Effects:
            - Updates self.requests dictionary
            - Modifies sequence buffer contents
            - May trigger buffer condensation
        """
        # Remove finished requests from tracking
        for req_id in scheduler_output.finished_req_ids:
            self.requests.pop(req_id, None)

        # Remove finished requests from sequence buffer
        removed_req_indices: list[int] = []
        for req_id in scheduler_output.finished_req_ids:
            req_index = self.sequence_buffer.remove_request(req_id)
            if req_index is not None:
                removed_req_indices.append(req_index)

        # Identify and remove unscheduled requests from buffer
        scheduled_req_ids = scheduler_output.num_scheduled_tokens.keys()
        cached_req_ids = self.sequence_buffer.req_id_to_index.keys()
        unscheduled_req_ids = cached_req_ids - scheduled_req_ids
        for req_id in unscheduled_req_ids:
            req_index = self.sequence_buffer.remove_request(req_id)
            assert req_index is not None
            removed_req_indices.append(req_index)

        # Add new requests to the tracking dictionary
        req_ids_to_add: list[str] = []
        for new_req_data in scheduler_output.scheduled_new_reqs:
            assert new_req_data.sampling_params is not None, "Pooling not supported in TPU"
            req_id = new_req_data.req_id
            self.requests[req_id] = CachedRequestState(
                req_id=req_id,
                prompt_token_ids=new_req_data.prompt_token_ids,
                sampling_params=new_req_data.sampling_params,
                generator=None,
                page_ids=new_req_data.page_ids,
                num_computed_tokens=new_req_data.num_computed_tokens,
                output_token_ids=[],
            )
            req_ids_to_add.append(req_id)

        # Process cached requests and prepare batch updates
        req_data = scheduler_output.scheduled_cached_reqs
        upd_req_indices: list[int] = []  # Indices of requests to update
        upd_num_computed_vals: list[int] = []  # New num_computed_tokens values
        batched_page_rows: list[tuple[int, tuple[list[int], ...]]] = []  # Page table updates

        # Update state for each cached request
        for i, req_id in enumerate(req_data.req_ids):
            req_state = self.requests.get(req_id)
            if req_state is None:
                continue

            nct = req_data.num_computed_tokens[i]  # New computed token count
            new_page_ids = req_data.new_page_ids[i]  # New pages allocated for this request
            resumed_from_preemption = req_data.resumed_from_preemption[i]
            req_state.num_computed_tokens = nct

            # Handle page IDs based on preemption status
            if not resumed_from_preemption:
                # Extend existing pages with new ones
                for page_ids, new_ids in zip(req_state.page_ids, new_page_ids, strict=False):
                    page_ids.extend(new_ids)
            else:
                # Replace pages entirely when resuming from preemption
                req_state.page_ids = new_page_ids

            # Check if request is already in sequence buffer
            req_index = self.sequence_buffer.req_id_to_index.get(req_id)
            if req_index is None:
                # Request not in buffer, needs to be added
                req_ids_to_add.append(req_id)
                continue

            # Collect updates for batched processing
            upd_req_indices.append(req_index)
            upd_num_computed_vals.append(int(nct))
            batched_page_rows.append((req_index, new_page_ids))

        # Batch update num_computed_tokens for efficiency
        if upd_req_indices:
            idx_arr = jnp.array(upd_req_indices, dtype=jnp.int32)
            val_arr = jnp.array(upd_num_computed_vals, dtype=jnp.int32)
            self.sequence_buffer.num_computed_tokens = self.sequence_buffer.num_computed_tokens.at[idx_arr].set(val_arr)

        # Batch update page tables using optimized batched operation
        if batched_page_rows:
            indices = [ix for ix, _ in batched_page_rows]
            pages_per_req = [ids for _, ids in batched_page_rows]
            self.sequence_buffer.page_table.append_rows_batch(pages_per_req, indices)

        # Add new/reinserted requests, reusing removed slots when possible
        removed_req_indices = sorted(removed_req_indices, reverse=True)
        for req_id in req_ids_to_add:
            req_state = self.requests[req_id]
            req_index = removed_req_indices.pop() if removed_req_indices else None
            self.sequence_buffer.add_request(req_state, req_index)

        # Condense sequence buffer to remove gaps from removed requests
        if removed_req_indices:
            self.sequence_buffer.condense(removed_req_indices)

        # Return whether any changes occurred that might affect scheduling
        has_changes = len(unscheduled_req_ids) > 0 or len(req_ids_to_add) > 0
        return has_changes

    def _prepare_inputs(self, scheduler_output: SchedulerOutput, start_index: int):
        assert scheduler_output.total_num_scheduled_tokens > 0
        num_reqs_total = self.sequence_buffer.num_reqs
        assert num_reqs_total > 0
        assert start_index < num_reqs_total

        # Collect scheduled counts for the current window (<= num_reqs_max_model_len)
        scheduled_list: list[int] = []
        for i in range(start_index, min(num_reqs_total, start_index + self.num_reqs_max_model_len)):
            rid = self.sequence_buffer.req_ids[i]
            scheduled_list.append(int(scheduler_output.num_scheduled_tokens.get(rid, 0)) if rid is not None else 0)

        # Trim trailing zeros to define active num_reqs in this window
        while scheduled_list and scheduled_list[-1] == 0:
            scheduled_list.pop()

        num_reqs = len(scheduled_list)
        end_index = start_index + (num_reqs if num_reqs > 0 else 0)

        # Fixed-size vector for JIT: [max_num_reqs]
        scheduled_full = jnp.zeros((self.max_num_reqs,), dtype=jnp.int32)
        if num_reqs > 0:
            scheduled_full = scheduled_full.at[:num_reqs].set(jnp.array(scheduled_list, dtype=jnp.int32))

        (
            self.input_ids_buf,
            self.position_ids_buf,
            self.query_start_loc_buf,
            self.seq_lens_buf,
            self.pages_tables_buf,
            self.slot_mapping_buf,
            logits_indices_full,
            padded_total,
            padded_num_reqs,
            num_kv_update_slices,
            padded_num_slices,
        ) = self._prepare_inputs_fn(
            scheduled_full,
            jnp.int32(num_reqs),
            self.sequence_buffer.num_computed_tokens,  # [max_num_reqs]
            self.sequence_buffer.token_ids,  # [max_num_reqs, max_model_len]
            self.sequence_buffer.page_table[0].get_array(),  # [max_num_reqs, max_pages_per_req]
            self.input_ids_buf,
            self.position_ids_buf,
            self.query_start_loc_buf,
            self.seq_lens_buf,
            self.pages_tables_buf,
            self.slot_mapping_buf,
        )

        # Views for model execution
        padded_total = int(padded_total)
        padded_num_reqs = int(padded_num_reqs)
        num_kv_update_slices = int(num_kv_update_slices)
        padded_num_slices = int(padded_num_slices)

        input_ids_view = self.input_ids_buf[:padded_total]
        position_ids_view = self.position_ids_buf[:padded_total]

        # PagesMetadata uses full fixed-size buffers; slot_mapping sliced to padded_num_slices
        attn_metadata = PagesMetadata(
            pages_tables=self.pages_tables_buf,
            slot_mapping=self.slot_mapping_buf[:, :padded_num_slices],
            context_lens=self.seq_lens_buf[: self.num_reqs_max_model_len],
            query_start_loc=self.query_start_loc_buf[: self.num_reqs_max_model_len + 1],
            num_seqs=jnp.array([num_reqs], dtype=jnp.int32),
            num_kv_update_slices=jnp.array([num_kv_update_slices], dtype=jnp.int32),
            num_slices_per_kv_cache_update_page=self.metadata.num_slices_per_kv_cache_update_page,
            page_size=self.metadata.page_size,
        )

        logits_indices = logits_indices_full[:padded_num_reqs]
        self._current_input_ids_view = input_ids_view
        self._current_position_ids_view = position_ids_view

        return attn_metadata, logits_indices, padded_num_reqs, num_reqs, end_index, padded_total

    @staticmethod
    @ejit(donate_argnums=(0, 1))
    def _apply_sampled_tokens_and_update(
        token_ids: jax.Array,  # [max_reqs, max_len]
        num_tokens: jax.Array,  # [max_reqs]
        selected_token_ids: jax.Array,  # [max_reqs] or [max_reqs, 1]
        scheduled_tokens: jax.Array,  # [max_reqs]
        req_num_tokens: jax.Array,  # [max_reqs]
        num_computed_tokens: jax.Array,  # [max_reqs]
        active_mask: jax.Array,  # [max_reqs] bool
        i_range: jax.Array,  # [max_reqs], reuse self.arange_np
    ) -> tuple[jax.Array, jax.Array, jax.Array, jax.Array]:
        """
        Shape-stable, fully jitted updater:
          - computes valid_mask
          - writes sampled token at [i, num_computed_tokens[i] + scheduled_tokens[i]]
          - returns updated token_ids, num_tokens, out_tokens[-1 for invalid], valid_mask
        """
        sampled_flat = selected_token_ids.squeeze(-1) if selected_token_ids.ndim > 1 else selected_token_ids
        seq_lens = num_computed_tokens + scheduled_tokens
        valid_mask = active_mask & (scheduled_tokens > 0) & (seq_lens >= req_num_tokens)

        # Static at trace time
        max_len_m1 = token_ids.shape[1] - 1
        j = jnp.clip(seq_lens, 0, max_len_m1)

        # masked "set" using add(delta) for better fusion
        current_vals = token_ids[i_range, j]
        delta = jnp.where(valid_mask, sampled_flat - current_vals, 0)
        token_ids = token_ids.at[(i_range, j)].add(delta)
        num_tokens = num_tokens + valid_mask.astype(num_tokens.dtype)

        out_tokens = jnp.where(valid_mask, sampled_flat, -1)
        return token_ids, num_tokens, out_tokens, valid_mask

    def execute_model(self, scheduler_output: SchedulerOutput) -> ModelRunnerOutput:
        """Execute the model on scheduled requests.

        Main entry point for model execution. Processes all scheduled requests
        in batches, handling state updates, input preparation, model execution,
        and token processing.

        The method handles:
        1. State synchronization with scheduler
        2. Batch-wise processing of requests
        3. Token generation and sampling
        4. Buffer updates and metrics logging

        Args:
            scheduler_output: Output from the scheduler containing:
                - Requests to process
                - Tokens to generate per request
                - Finished/new/cached request information

        Returns:
            ModelRunnerOutput: Contains:
                - req_ids: List of processed request IDs
                - sampled_token_ids: Generated tokens per request
                - logprobs: Log probabilities (if requested)
                - Timing and debugging information

        Note:
            The method processes requests in batches when they exceed
            the maximum model length, ensuring all requests are handled
            efficiently without exceeding memory constraints.
        """
        execution_start_time = time.time()
        logger.debug(f"Starting model execution with {scheduler_output.total_num_scheduled_tokens} scheduled tokens")

        logger.debug("Updating internal states based on scheduler output")
        updating_states_start = time.time()
        self._update_states(scheduler_output)
        updating_states_time = time.time() - updating_states_start

        if not scheduler_output.total_num_scheduled_tokens:
            logger.debug("No tokens scheduled, returning empty output")
            return ModelRunnerOutput(
                req_ids=[],
                req_id_to_index={},
                sampled_token_ids=[],
                spec_token_ids=None,
                logprobs=None,
                prompt_logprobs_dict={},
                finished_sending=None,
                finished_recving=None,
                num_nans_in_logits=None,
            )

        start_index = 0
        combined_selected_tokens: list[jax.Array] = []
        batch_count = 0
        total_prepare_time = 0.0
        total_exec_time = 0.0

        logger.debug(f"Processing {self.sequence_buffer.num_reqs} requests in batches")
        while start_index < self.sequence_buffer.num_reqs:
            batch_count += 1
            logger.debug(f"Batch {batch_count}: Preparing inputs starting from index {start_index}")
            prepare_start = time.time()
            (
                cache_metadata,
                logits_indices,
                padded_num_reqs,
                num_reqs,
                end_index,
                padded_total,
            ) = self._prepare_inputs(scheduler_output, start_index)
            prepare_time = time.time() - prepare_start
            total_prepare_time += prepare_time

            exec_start = time.time()
            selected_token_ids, logits = self.executor_manager.execute(
                self._current_input_ids_view,
                self._current_position_ids_view,
                cache_metadata,
                logits_indices,
                ModelRunnerSamplingMetadata.from_sequence_buffer(self.sequence_buffer, padded_num_reqs),
                padded_num_reqs,
            )
            # Ensure timing reflects real device work
            selected_token_ids = jax.block_until_ready(selected_token_ids)[:num_reqs]
            exec_time = time.time() - exec_start
            total_exec_time += exec_time

            combined_selected_tokens.append(selected_token_ids)
            start_index = end_index

        selected_token_ids = jnp.concatenate(combined_selected_tokens, axis=0)
        logger.debug(f"Processed {batch_count} batches, generated {selected_token_ids.shape[0]} tokens")

        logger.debug("Processing sampled tokens and updating buffers")
        processing_token_start = time.time()
        result = self._process_sampled_tokens(
            selected_token_ids,
            scheduler_output,
            execution_start_time,
        )
        processing_token_time = time.time() - processing_token_start
        total_time = time.time() - execution_start_time

        self.log_it(
            f"Model execution in {total_exec_time:.3f}s "
            f"Input preparation in {total_prepare_time:.3f}s "
            f"execution completed in {total_time:.3f}s "
            f"processing token in {processing_token_time:.3f}s "
            f"updating states in {updating_states_time:.3f}s"
        )
        return result

    @staticmethod
    @ejit
    def _update_token_buffers_optimized(
        token_ids: jax.Array,  # [max_reqs, max_len]
        num_tokens: jax.Array,  # [max_reqs]
        update_indices: jax.Array,  # [K]
        new_token_ids: jax.Array,  # [K] or [K, 1]
        seq_lens: jax.Array,  # [K]
    ) -> tuple[jax.Array, jax.Array]:
        """Vectorized token buffer updates."""
        new_token_ids = new_token_ids.squeeze(-1) if new_token_ids.ndim == 2 else new_token_ids
        token_ids = token_ids.at[(update_indices, seq_lens)].set(new_token_ids)
        num_tokens = num_tokens.at[update_indices].add(1)
        return token_ids, num_tokens

    @staticmethod
    @jax.jit
    def _prepare_token_updates(
        selected_token_ids: jax.Array,  # [max_reqs] or [max_reqs, 1]
        num_computed_tokens: jax.Array,  # [max_reqs]
        scheduled_tokens: jax.Array,  # [max_reqs]
        req_num_tokens: jax.Array,  # [max_reqs]
        active_mask: jax.Array,  # [max_reqs] boolean mask for active requests
    ) -> tuple[jax.Array, jax.Array, jax.Array, jax.Array]:
        """Prepare token update indices and values using JAX operations.

        Returns:
            - valid_mask: [max_reqs] boolean mask for valid updates
            - update_indices: [max_reqs] indices to update (masked with -1 for invalid)
            - update_tokens: [max_reqs] token values (masked with 0 for invalid)
            - update_seq_lens: [max_reqs] sequence lengths (masked with 0 for invalid)
        """
        sampled_flat = selected_token_ids.squeeze(-1) if selected_token_ids.ndim > 1 else selected_token_ids
        seq_lens = num_computed_tokens + scheduled_tokens
        has_scheduled = scheduled_tokens > 0
        meets_length = seq_lens >= req_num_tokens
        valid_mask = active_mask & has_scheduled & meets_length
        max_reqs = selected_token_ids.shape[0]
        indices = jnp.arange(max_reqs, dtype=jnp.int32)
        update_indices = jnp.where(valid_mask, indices, -1)
        update_tokens = jnp.where(valid_mask, sampled_flat, 0)
        update_seq_lens = jnp.where(valid_mask, seq_lens, 0)
        return valid_mask, update_indices, update_tokens, update_seq_lens

    @staticmethod
    @jax.jit
    def _extract_valid_sampled_tokens(
        selected_token_ids: jax.Array,  # [num_reqs] or [num_reqs, 1]
        valid_mask: jax.Array,  # [num_reqs] boolean
        num_reqs: int,
    ) -> jax.Array:
        """Extract valid sampled tokens as a 2D array for output.

        Returns:
            - valid_tokens: [num_reqs, 1] array with -1 for invalid tokens
        """
        sampled_flat = selected_token_ids.squeeze(-1) if selected_token_ids.ndim > 1 else selected_token_ids
        sampled_flat = sampled_flat[:num_reqs]
        valid_tokens = jnp.where(valid_mask, sampled_flat, -1)
        return valid_tokens.reshape(-1, 1)

    def _process_sampled_tokens(
        self,
        selected_token_ids: jax.Array,  # [num_reqs, 1] typically
        scheduler_output: SchedulerOutput,
        execution_start_time: float,
    ) -> ModelRunnerOutput:
        """Process sampled tokens and update buffers with async host copy."""
        num_reqs = self.sequence_buffer.num_reqs
        logger.debug(f"Processing {selected_token_ids.shape[0]} sampled tokens for {num_reqs} requests")
        req_ids_window = list(self.sequence_buffer.req_ids[:num_reqs])
        scheduled_list = [
            int(scheduler_output.num_scheduled_tokens.get(rid, 0)) if rid is not None else 0 for rid in req_ids_window
        ]
        req_num_tokens_list = [
            int(getattr(self.requests.get(rid, None), "num_tokens", 0)) if rid is not None else 0
            for rid in req_ids_window
        ]
        active_list = [rid is not None for rid in req_ids_window]

        scheduled_tokens_arr = (
            jnp.zeros((self.max_num_reqs,), dtype=jnp.int32)
            .at[:num_reqs]
            .set(jnp.array(scheduled_list, dtype=jnp.int32))
        )
        req_num_tokens_arr = (
            jnp.zeros((self.max_num_reqs,), dtype=jnp.int32)
            .at[:num_reqs]
            .set(jnp.array(req_num_tokens_list, dtype=jnp.int32))
        )
        active_mask = jnp.zeros((self.max_num_reqs,), dtype=bool).at[:num_reqs].set(jnp.array(active_list, dtype=bool))

        if selected_token_ids.shape[0] < self.max_num_reqs:
            pad_w = self.max_num_reqs - selected_token_ids.shape[0]
            if selected_token_ids.ndim == 1:
                selected_token_ids = jnp.pad(selected_token_ids, (0, pad_w), constant_values=0)
            else:
                selected_token_ids = jnp.pad(selected_token_ids, ((0, pad_w), (0, 0)), constant_values=0)

        self.sequence_buffer.token_ids, self.sequence_buffer.num_tokens, out_tokens, valid_mask = (
            self._apply_sampled_tokens_and_update(
                self.sequence_buffer.token_ids,
                self.sequence_buffer.num_tokens,
                selected_token_ids,
                scheduled_tokens_arr,
                req_num_tokens_arr,
                self.sequence_buffer.num_computed_tokens,
                active_mask,
                self.arange_np,  # prebuilt in _setup_variables
            )
        )

        out_tokens.copy_to_host_async()
        valid_mask.copy_to_host_async()

        prompt_logprobs_dict: dict[str, LogprobsTensors | None] = {
            rid: None for rid in req_ids_window if rid is not None
        }

        out_tokens_np, valid_mask_np = jax.device_get((out_tokens[:num_reqs], valid_mask[:num_reqs]))
        sampled_token_ids: list[list[int]] = []
        req_ids: list[str] = []
        for i, rid in enumerate(req_ids_window):
            if rid is None:
                continue
            req_ids.append(rid)
            if valid_mask_np[i]:
                tid = int(out_tokens_np[i])
                sampled_token_ids.append([tid])
                rs = self.requests.get(rid)
                if rs:
                    rs.output_token_ids.append(tid)
            else:
                sampled_token_ids.append([])

        metrics_collector = get_metrics_collector()
        if metrics_collector:
            metrics_collector.record_runner_metrics(
                execution_time=time.time() - execution_start_time,
                batch_size=len(req_ids),
                num_tokens=scheduler_output.total_num_scheduled_tokens,
            )

        return ModelRunnerOutput(
            req_ids=req_ids,
            req_id_to_index=self.sequence_buffer.req_id_to_index,
            sampled_token_ids=sampled_token_ids,
            spec_token_ids=None,
            logprobs=None,
            prompt_logprobs_dict=prompt_logprobs_dict,
            finished_sending=None,
            finished_recving=None,
        )
