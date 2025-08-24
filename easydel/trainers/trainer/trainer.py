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
import typing as tp

import jax
import jax.experimental
import jax.lib
from eformer.escale import with_sharding_constraint
from jax.sharding import NamedSharding, PartitionSpec

from easydel.infra.base_state import EasyDeLState
from easydel.infra.errors import EasyDeLBreakRequest, EasyDeLTimerError
from easydel.infra.loss_utils import LossMetrics
from easydel.utils.compiling_utils import ejit
from easydel.utils.helpers import capture_time, get_logger

from ..base_trainer import BaseTrainer, TrainerConfigureFunctionOutput
from ..trainer_protocol import BaseProgressBar, MetricsTracker, StepMetrics, TrainerOutput
from ._fn import evaluation_step, training_step

logger = get_logger(__name__)


class Trainer(BaseTrainer):
    def create_grain_collect_function(
        self,
        max_sequence_length: int,
        truncation_mode: tp.Literal["keep_end", "keep_start"] = "keep_end",
    ) -> tp.Callable:
        """
        Creates a collate/collect function to process batches of data for training or evaluation.

        This function returns a callable that takes a batch (a list of dictionaries) and converts it
        into a dictionary of JAX arrays. For models of class "ForCausalLMLoss", it also performs
        truncation (either keeping the end or the start of the sequence) so that each sequence does not
        exceed the specified maximum length.

        Args:
            max_sequence_length (int): The maximum allowed sequence length.
            truncation_mode (tp.Literal["keep_end", "keep_start"], optional):
                Determines whether to keep the end or the start of the sequence when truncating.
                Defaults to "keep_end".

        Returns:
            tp.Callable: A function that takes a batch (list of dicts) and returns a processed dict of arrays.
        """

        def collate_fn(batch):
            return batch

        return collate_fn

    def create_tfds_collect_function(
        self,
        max_sequence_length: int,
        truncation_mode: tp.Literal["keep_end", "keep_start"] = "keep_end",
    ) -> tp.Callable:
        """
        Creates a collate/collect function to process batches of data for training or evaluation.

        This function returns a callable that takes a batch (a list of dictionaries) and converts it
        into a dictionary of JAX arrays. For models of class "ForCausalLMLoss", it also performs
        truncation (either keeping the end or the start of the sequence) so that each sequence does not
        exceed the specified maximum length.

        Args:
            max_sequence_length (int): The maximum allowed sequence length.
            truncation_mode (tp.Literal["keep_end", "keep_start"], optional):
                Determines whether to keep the end or the start of the sequence when truncating.
                Defaults to "keep_end".

        Returns:
            tp.Callable: A function that takes a batch (list of dicts) and returns a processed dict of arrays.
        """

        def collate_fn(batch):
            results = {}
            for key in batch[0].keys():
                data_sample = batch[0][key]
                try:
                    data_sample = jax.numpy.array(data_sample)
                except TypeError:
                    continue
                if self.model.lossfn_type == "ForCausalLM":
                    if truncation_mode == "keep_end":
                        corrected_sequence = [jax.numpy.array(f[key])[..., -max_sequence_length:] for f in batch]
                    else:
                        corrected_sequence = [jax.numpy.array(f[key])[..., :max_sequence_length] for f in batch]
                    results[key] = jax.numpy.stack(corrected_sequence)
                else:
                    corrected_sequence = [jax.numpy.array(f[key]) for f in batch]
                    results[key] = jax.numpy.stack(corrected_sequence)
            return results

        return collate_fn

    def create_collect_function(
        self,
        max_sequence_length: int,
        truncation_mode: tp.Literal["keep_end", "keep_start"],
    ) -> tp.Callable:
        """
        Creates a function to collect and process batches of data for training or evaluation.

        This function handles padding or truncating sequences to the specified `max_sequence_length`
        based on the chosen `truncation_mode`.

        Args:
            max_sequence_length (int): The maximum allowed sequence length.
            truncation_mode (typing.tp.Literal["keep_end", "keep_start"], optional):
                The truncation mode. Defaults to "keep_end".

        Returns:
            tp.Callable: A function that takes a batch of data and returns a processed batch.
        """
        return (
            self.create_grain_collect_function(
                max_sequence_length=max_sequence_length,
                truncation_mode=truncation_mode,
            )
            if self.arguments.use_grain
            else self.create_tfds_collect_function(
                max_sequence_length=max_sequence_length,
                truncation_mode=truncation_mode,
            )
        )

    def configure_functions(self) -> TrainerConfigureFunctionOutput:
        """
        Configures and JIT-compiles the training and evaluation step functions.

        This method prepares the functions that will be used during training and evaluation.
        It sets up sharding for the model parameters and optimizer state, JIT-compiles the
        training and evaluation functions with the appropriate static arguments and sharding
        constraints, and also sets up the checkpoint manager.

        Returns:
            TrainerConfigureFunctionOutput: An object containing:
                - sharded_training_step_function: The compiled training step function.
                - sharded_evaluation_step_function: The compiled evaluation step function.
                - mesh: The device mesh used for computation.
                - checkpoint_manager: The checkpointer for saving/loading model state.
        """
        empty_sharding = jax.sharding.NamedSharding(spec=PartitionSpec(), mesh=self.model.mesh)
        self._train_shared_fn_static_args = (
            self.arguments.loss_config,
            self.scheduler,
            self.arguments.step_partition_spec,
            self.arguments.gradient_accumulation_steps,
        )
        sharded_training_step_function = ejit(
            training_step,
            static_argnums=(2, 3, 4, 5),
            in_shardings=(self.state_shardings, empty_sharding),
            out_shardings=(self.state_shardings, empty_sharding),
            donate_argnums=(0,),
        )

        self._eval_shared_fn_static_args = (
            self.arguments.loss_config,
            self.arguments.step_partition_spec,
        )
        sharded_evaluation_step_function = ejit(
            evaluation_step,
            static_argnums=(2, 3),
            in_shardings=(self.state_shardings, empty_sharding),
            out_shardings=(empty_sharding),
        )

        mesh = self.model.mesh
        self.arguments.ensure_checkpoint_path()
        checkpoint_manager = self.arguments.get_streaming_checkpointer()

        return TrainerConfigureFunctionOutput(
            sharded_training_step_function=sharded_training_step_function,
            sharded_evaluation_step_function=sharded_evaluation_step_function,
            mesh=mesh,
            checkpoint_manager=checkpoint_manager,
        )

    def _all_gather(self, arr: jax.Array) -> jax.Array:
        return jax.device_put(arr, NamedSharding(self.model.mesh, PartitionSpec()))

    def _one_to_all(self, arr: jax.Array) -> jax.Array:
        with self.mesh:
            arr = with_sharding_constraint(arr, PartitionSpec(None))
        return arr

    def _run_training_loop(
        self,
        state: EasyDeLState,
        metrics_tracker: MetricsTracker,
        step_metrics: StepMetrics,
    ):
        """
        Implements the core training loop.

        Iterates over the training epochs and steps, executing training steps and updating metrics.
        A progress bar is created to track the training process. If the process index is not 0
        (and logging on all workers is disabled), the progress bar is disabled.

        Args:
            state (EasyDeLState): The initial model state.
            metrics_tracker (MetricsTracker): Tracker for accumulating and updating training metrics.
            step_metrics (StepMetrics): Object to calculate metrics per training step.

        Returns:
            A tuple containing the final training output (e.g., updated state and metrics) and any run exception.
        """
        disabled = False
        if jax.process_index() != 0 and not self.arguments.log_all_workers:
            disabled = True
        pbar = self.create_progress_bar(
            total=self.max_training_steps,
            disabled=disabled,
            desc="training process",
        )
        train_iter = iter(self.dataloader_train)
        try:
            run_exception = None
            with self.mesh:
                for epoch in range(self.arguments.num_train_epochs):
                    try:
                        if jax.process_index() == 0:
                            logger.info(f"DEBUG: Starting epoch {epoch+1}/{self.arguments.num_train_epochs}")
                    except Exception:
                        ...
                    state, run_exception, train_iter = self._train_epoch(
                        state=state,
                        train_dataset=self.dataloader_train,
                        train_iter=train_iter,
                        metrics_tracker=metrics_tracker,
                        step_metrics=step_metrics,
                        pbar=pbar,
                        epoch=epoch,
                    )

                    current_step = int(jax.device_get(state.step))
                    if current_step >= self.max_training_steps:
                        break
                    if run_exception is not None:
                        break
            return self._prepare_training_output(state=state, run_exception=run_exception), run_exception
        finally:
            pbar.close()

    def _run_evaluation(
        self,
        state: EasyDeLState,
        metrics_tracker: MetricsTracker,
        step_metrics: StepMetrics,
    ):
        """
        Implements the core evaluation loop.

        Iterates over the evaluation dataset, performing evaluation steps, updating metrics, and yielding metrics
        for each evaluation step. A progress bar is used to indicate evaluation progress.

        Args:
            state (EasyDeLState): The model state used for evaluation.
            metrics_tracker (MetricsTracker): Tracker for accumulating evaluation metrics.
            step_metrics (StepMetrics): Object to calculate metrics per evaluation step.

        Yields:
            dict: A dictionary containing evaluation metrics for each step.
        """
        disabled = False
        if jax.process_index() != 0 and not self.arguments.log_all_workers:
            disabled = True
        pbar = self.create_progress_bar(
            total=self.max_evaluation_steps,
            disabled=disabled,
            desc="evaluation process",
        )

        eval_iter = iter(self.dataloader_eval)
        try:
            with self.mesh:
                yield from self._eval_epoch(
                    state=state,
                    eval_dataset=self.dataloader_eval,
                    eval_iter=eval_iter,
                    metrics_tracker=metrics_tracker,
                    step_metrics=step_metrics,
                    pbar=pbar,
                )
        finally:
            pbar.close()

    def _train_epoch(
        self,
        state: EasyDeLState,
        train_dataset,
        train_iter,
        metrics_tracker: MetricsTracker,
        step_metrics: StepMetrics,
        pbar: BaseProgressBar,
        epoch: int,
    ):
        """
        Performs training over one epoch.

        Iterates over the training dataset for a fixed number of steps in the epoch.
        Each step fetches a batch, applies data collation, executes a training step,
        updates metrics, logs metrics, and optionally saves checkpoints.

        Args:
            state (EasyDeLState): The current model state.
            train_dataset: The training dataset (or an iterator over it).
            metrics_tracker (MetricsTracker): Tracker to update and store training metrics.
            step_metrics (StepMetrics): Object to calculate step-level metrics.
            pbar (BaseProgressBar): Progress bar instance for displaying training progress.
            epoch (int): The current epoch index.

        Returns:
            A tuple of (updated state, any exception encountered during the run and train iterator).
        """
        data_collator = self.data_collator
        if data_collator is None:

            def data_collator(x):
                return x

        # Ensure at least one iteration and avoid ZeroDivision
        total_epochs = max(1, int(self.arguments.num_train_epochs))
        total_steps = int(self.max_training_steps) if self.max_training_steps is not None else 0
        iters = max(1, total_steps // total_epochs) if total_steps > 0 else max(1, self.max_training_steps or 1)
        run_exception = None
        for _ in range(iters):
            current_step = int(jax.device_get(state.step))
            try:
                batch, train_iter = self._get_next_batch(train_iter, train_dataset)
                if self._should_skip_step(current_step):
                    pbar.update(1)
                    continue
                step_metrics.start_step()
                state = self.on_step_start(state=state, step=current_step)
            except (KeyboardInterrupt, EasyDeLTimerError, EasyDeLBreakRequest, StopIteration) as exect:
                run_exception = exect
                return state, run_exception, train_iter

            # Execute training step
            with self.train_tracker.trace_compilation():
                with capture_time() as execution_time:
                    state, metrics, run_exception = self._execute_train_step(state=state, batch=data_collator(batch))
                    metrics.execution_time = execution_time()
                    current_step = int(jax.device_get(state.step))
            # Update and log metrics
            try:
                mean_loss, mean_accuracy = metrics_tracker.update(
                    loss=metrics.loss,
                    accuracy=metrics.accuracy,
                    step=current_step,
                )
                metrics = self.apply_training_hooks(metrics=metrics)
                train_metrics = step_metrics.calculate(
                    metrics=metrics,
                    current_step=current_step,
                    learning_rate=self.scheduler(current_step)
                    if self.scheduler is not None
                    else self.arguments.learning_rate,
                    epoch=epoch,
                    flops_per_token=self._backward_flops_per_token,
                    extra_flops_per_token=self._extra_backward_flops_per_token,
                    batch_size=self.training_batch_size,
                    seq_length=self.arguments.max_sequence_length,
                    mean_loss=mean_loss,
                    mean_accuracy=mean_accuracy,
                    mode="train",
                )
                state, metrics = self.on_step_end(
                    state=state,
                    metrics=metrics,
                    step=current_step,
                )
                self.log_metrics(
                    metrics=train_metrics,
                    pbar=pbar,
                    step=current_step,
                    mode="train",
                )
                self.log_weight_distribution(state=state, step=current_step)
                # Save checkpoint if needed
                if self._should_save_checkpoint(current_step):
                    _ = self._save_state(
                        state=state,
                        milestone=True,
                        save_directory=self.arguments.save_directory,
                    )
                if self._should_run_evaluation(current_step):
                    for _ in self.eval(model_state=state):
                        ...
            except (KeyboardInterrupt, EasyDeLTimerError, EasyDeLBreakRequest, TypeError) as exect:
                run_exception = exect if run_exception is None else run_exception
                return state, run_exception, train_iter
            if run_exception is not None:
                break
        return state, run_exception, train_iter

    def _eval_epoch(
        self,
        state: EasyDeLState,
        eval_dataset,
        eval_iter,
        metrics_tracker: MetricsTracker,
        step_metrics: StepMetrics,
        pbar: BaseProgressBar,
    ):
        """
        Performs evaluation over one epoch.

        Iterates over the evaluation dataset, processes each batch through the evaluation step,
        updates and logs metrics, and yields the evaluation metrics.

        Args:
            state (EasyDeLState): The model state used for evaluation.
            eval_dataset: The evaluation dataset (or an iterator over it).
            metrics_tracker (MetricsTracker): Tracker for accumulating evaluation metrics.
            step_metrics (StepMetrics): Object to calculate step-level metrics.
            pbar (BaseProgressBar): Progress bar instance for displaying evaluation progress.

        Yields:
            dict: A dictionary of evaluation metrics for each evaluation step.
        """
        assert eval_dataset is not None, "Make sure to pass eval dataset to trainer or set `do_eval` to `False`."
        data_collator = self.data_collator
        if data_collator is None:

            def data_collator(x):
                return x

        for current_step in range(1, self.max_evaluation_steps + 1):
            try:
                batch, eval_iter = self._get_next_batch(eval_iter, eval_dataset)
                step_metrics.start_step()
                with self.evalu_tracker.trace_compilation():
                    with capture_time() as execution_time:
                        metrics = self._execute_eval_step(state, data_collator(batch))
                        metrics.execution_time = execution_time()
                mean_loss, mean_accuracy = metrics_tracker.update(
                    metrics.loss,
                    metrics.accuracy,
                    current_step,
                )
                eval_metrics = step_metrics.calculate(
                    metrics=metrics,
                    current_step=current_step,
                    learning_rate=0.000,
                    epoch=0,
                    flops_per_token=self._forward_flops_per_token,
                    extra_flops_per_token=self._extra_forward_flops_per_token,
                    batch_size=self.evaluation_batch_size,
                    seq_length=self.arguments.max_sequence_length,
                    mean_loss=mean_loss,
                    mean_accuracy=mean_accuracy,
                    mode="eval",
                )
                self.log_metrics(
                    metrics=eval_metrics,
                    pbar=pbar,
                    step=current_step,
                    mode="eval",
                )
                yield eval_metrics
            except (KeyboardInterrupt, EasyDeLTimerError, EasyDeLBreakRequest, TypeError):
                break
            except Exception as ex:
                try:
                    print(
                        f"DEBUG: Unexpected exception during evaluation at step={current_step}: {type(ex).__name__}: {ex}"
                    )
                except Exception:
                    ...
                break

    def _execute_eval_step(self, state, batch) -> LossMetrics:
        """
        Executes a single evaluation step.

        Args:
            state: The current model state.
            batch: A processed batch of evaluation data.

        Returns:
            LossMetrics: The loss metrics computed by the sharded evaluation step function.
        """
        batch, informations = self._preprocess_batch_input(
            state=state,
            batch=batch,
            is_train=False,
        )
        metrics = self.sharded_evaluation_step_function(
            state,
            batch,
            *self._eval_shared_fn_extra_args,
            *self._eval_shared_fn_static_args,
        )
        if len(informations) != 0:
            if metrics.other_metrics is not None:
                informations.update(metrics.other_metrics)
            metrics = metrics.replace(other_metrics=informations)
        return metrics

    def _execute_train_step(
        self,
        state,
        batch,
    ) -> tuple[EasyDeLState, LossMetrics, BaseException | None]:
        """
        Executes a single training step.

        This function optionally updates the model's pruning module before and after the gradient step.
        It then calls the sharded training step function to compute the gradients and update the state.
        If an exception occurs (e.g. KeyboardInterrupt, timer error, or break request), it is captured and returned.

        Args:
            state: The current model state.
            batch: A processed batch of training data.

        Returns:
            A tuple containing:
                - The updated model state.
                - The computed LossMetrics.
                - An exception instance if one was raised during execution; otherwise, None.
        """
        if self.pruning_module is not None:
            state = state.replace(
                graphstate=self.pruning_module.pre_forward_update(
                    state.graphstate,
                    state.opt_state,
                )
            )
        metrics = LossMetrics()
        try:
            batch, informations = self._preprocess_batch_input(
                state=state,
                batch=batch,
                is_train=True,
            )

            state, metrics = jax.block_until_ready(
                self.sharded_training_step_function(
                    state,
                    batch,
                    *self._train_shared_fn_extra_args,
                    *self._train_shared_fn_static_args,
                )
            )

            if len(informations) != 0:
                if metrics.other_metrics is not None:
                    informations.update(metrics.other_metrics)
                metrics = metrics.replace(other_metrics=informations)

            # Apply post-gradient updates via the pruning module, if present.
            if self.pruning_module is not None:
                state = state.replace(
                    graphstate=self.pruning_module.post_gradient_update(
                        state.graphstate,
                        state.opt_state,
                    )
                )
            return state, metrics, None
        except (
            KeyboardInterrupt,
            EasyDeLTimerError,
            EasyDeLBreakRequest,
            TypeError,
        ) as run_exception:
            return state, metrics, run_exception
        

    def _finalize_training(self, output, run_exception):
        """
        Finalizes the training process and prepares the output.

        If evaluation is enabled, this method runs an additional evaluation pass before finishing.
        It then calls the finish method to perform any cleanup and returns the final output.

        Args:
            output: The output object containing the final state and metrics.
            run_exception: Any exception that was encountered during training.

        Returns:
            The final output object.
        """
        try:
            if self.arguments.do_eval:
                for _ in self.eval(output.state):
                    ...
        except RuntimeError:
            logger.info("Caught RuntimeError from eval function (mostly due to `StopIteration` being called manually)")
        self.finish()
        return output

    def train(self) -> TrainerOutput:
        """
        Executes the complete training process.

        This method sets up initial metrics and logging, runs the training loop, and finalizes training.
        It calls the training hook at the beginning and returns a TrainerOutput object at the end.

        Returns:
            TrainerOutput: An object containing the final training state, metrics, and any additional outputs.
        """
        self.start_training_hook()
        state = self.model_state
        metrics_tracker = MetricsTracker()
        step_metrics = StepMetrics(self.arguments)
        # Setup initial metrics and logging.
        self._setup_initial_metrics(state)
        output, run_exception = self._run_training_loop(
            state=self.model_state,
            metrics_tracker=metrics_tracker,
            step_metrics=step_metrics,
        )
        return self._finalize_training(output, run_exception)

    def eval(self, model_state: EasyDeLState) -> tp.Iterator[dict]:
        """
        Evaluates the model using the provided model state.

        This method iterates over the evaluation dataset, performs forward passes, calculates evaluation metrics,
        logs the metrics, and yields the metrics for each evaluation step.

        Args:
            model_state (EasyDeLState): The state of the model (including parameters and configuration)
                                        to be used for evaluation.

        Yields:
            Iterator[dict]: An iterator yielding a dictionary of evaluation metrics for each evaluation step.

        Raises:
            AssertionError: If the evaluation dataloader is not set.
        """
        self.start_evaluation_hook()
        try:
            metrics_tracker = MetricsTracker()
            step_metrics = StepMetrics(self.arguments)
            yield from self._run_evaluation(
                state=model_state,
                metrics_tracker=metrics_tracker,
                step_metrics=step_metrics,
            )
        except RuntimeError:
            # In multi-host evaluation, RuntimeError might be raised; catch and continue.
            ...
