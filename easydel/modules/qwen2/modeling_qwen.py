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


from functools import partial

import chex
import jax
import jax.numpy as jnp
from eformer import common_types
from eformer.escale import apply_logical_sharding
from flax import nnx as nn

from easydel.infra.base_module import EasyDeLBaseModule
from easydel.infra.factory import TaskType, register_module
from easydel.infra.modeling_outputs import (
    AttentionLayerOutput,
    BaseModelOutput,
    CausalLMOutput,
    DecoderLayerOutput,
    SequenceClassifierOutput,
)
from easydel.infra.utils import ACT2FN, auto_remat, block_wise_ffn, get_dot_general_by_bits
from easydel.layers.attention import AttentionModule, FlexibleAttentionModule
from easydel.layers.caching import (
    PagesCache,
    PagesCacheView,
    PagesMetadata,
    TransformerCache,
    TransformerCacheView,
    TransformerMetadata,
)
from easydel.layers.linear import ParallelLinear
from easydel.layers.norms import RMSNorm as RMSNorm

from .qwen_configuration import Qwen2Config


class Qwen2MLP(nn.Module):
    """Qwen2 MLP module.

    This module implements the feed-forward network (MLP) used in the Qwen2 model.
    It uses a Gated Linear Unit (GLU) structure with SiLU activation and includes dropout.

    Attributes:
        config (Qwen2Config): Configuration object for the model.
        dtype (jnp.dtype): Data type for computations.
        param_dtype (jnp.dtype): Data type for parameters.
        precision (jax.lax.PrecisionLike): Precision setting for JAX operations.
        gate_proj (ParallelLinear): Linear layer for the GLU gate.
        down_proj (ParallelLinear): Linear layer for the down projection.
        up_proj (ParallelLinear): Linear layer for the GLU value.
        dropout (nn.Dropout): Dropout layer applied to the output.
        act_fn (callable): Activation function (SiLU).
    """

    def __init__(
        self,
        config: Qwen2Config,
        dtype: jnp.dtype = jnp.bfloat16,
        param_dtype: jnp.dtype = jnp.bfloat16,
        precision: jax.lax.PrecisionLike = None,
        *,
        rngs: nn.Rngs,
    ):
        """Initializes the Qwen2MLP module.

        Args:
            config (Qwen2Config): The configuration object for the Qwen2 model.
            dtype (jnp.dtype): Data type for computation. Defaults to jnp.float32.
            param_dtype (jnp.dtype): Data type for parameters. Defaults to jnp.float32.
            precision (jax.lax.PrecisionLike): Precision setting for JAX operations. Defaults to None.
            rngs (nn.Rngs): Random number generators.
        """
        self.config = config
        self.dtype = dtype
        self.param_dtype = param_dtype
        self.precision = precision
        linear_class = partial(
            ParallelLinear,
            dtype=dtype,
            param_dtype=param_dtype,
            use_bias=False,
            kernel_init=jax.nn.initializers.normal(config.initializer_range),
            precision=precision,
            rngs=rngs,
            **get_dot_general_by_bits(config.bits, config.easy_method),
        )
        self.gate_proj = linear_class(
            config.hidden_size,
            config.intermediate_size,
            rngs=rngs,
        )
        self.down_proj = linear_class(
            config.intermediate_size,
            config.hidden_size,
            rngs=rngs,
        )
        self.up_proj = linear_class(
            config.hidden_size,
            config.intermediate_size,
            rngs=rngs,
        )
        self.dropout = nn.Dropout(rate=self.config.resid_pdrop, rngs=rngs)
        self.act_fn = ACT2FN[self.config.hidden_act]

    def __call__(self, hidden_states: jnp.ndarray) -> jnp.ndarray:
        """Forward pass of the Qwen2MLP module.

        Args:
            hidden_states (jnp.ndarray): Input hidden states.

        Returns:
            jnp.ndarray: Output hidden states after MLP transformation.
        """
        hidden_states = apply_logical_sharding(
            hidden_states,
            dynamic_axes=common_types.HiddenStateSharding,
            partition_manager=self.config.partition_manager,
        )
        gate = self.act_fn(self.gate_proj(hidden_states))
        up = self.up_proj(hidden_states)
        hidden_states = self.down_proj(gate * up)
        hidden_states = self.dropout(hidden_states)
        hidden_states = apply_logical_sharding(
            hidden_states,
            dynamic_axes=common_types.HiddenStateSharding,
            partition_manager=self.config.partition_manager,
        )
        return hidden_states


class Qwen2Attention(AttentionModule):
    """Qwen2 Attention module.

    This module implements the multi-head attention mechanism used in the Qwen2 model.
    It supports Grouped Query Attention (GQA) and Rotary Position Embeddings (RoPE).
    It also includes a residual dropout layer.

    Attributes:
        config (Qwen2Config): Configuration object for the model.
        dtype (jnp.dtype): Data type for computations.
        param_dtype (jnp.dtype): Data type for parameters.
        precision (jax.lax.PrecisionLike): Precision setting for JAX operations.
        rngs (nn.Rngs): Random number generators.
        hidden_size (int): Dimensionality of the hidden states.
        head_dim (int): Dimensionality of each attention head.
        num_key_value_groups (int): Number of query head groups for each key/value head.
        q_proj (ParallelLinear): Linear layer for query projection.
        k_proj (ParallelLinear): Linear layer for key projection.
        v_proj (ParallelLinear): Linear layer for value projection.
        o_proj (ParallelLinear): Linear layer for the output projection.
        attention_performer (FlexibleAttentionModule): Module to perform the core attention computation.
        resid_dropout (nn.Dropout): Dropout applied to the residual connection within the attention block
            (if config enables).
        rotary (RoPE): Rotary position embedding module.
    """

    def __init__(
        self,
        config: Qwen2Config,
        layer_idx: int,
        dtype: jnp.dtype = jnp.bfloat16,
        param_dtype: jnp.dtype = jnp.bfloat16,
        precision: jax.lax.PrecisionLike = None,
        *,
        rngs: nn.Rngs,
    ):
        """Initializes the Qwen2Attention module.

        Args:
            config (Qwen2Config): The configuration object for the Qwen2 model.
            dtype (jnp.dtype): Data type for computation. Defaults to jnp.float32.
            param_dtype (jnp.dtype): Data type for parameters. Defaults to jnp.float32.
            precision (jax.lax.PrecisionLike): Precision setting for JAX operations. Defaults to None.
            rngs (nn.Rngs): Random number generators.

        Raises:
            ValueError: If `hidden_size` is not divisible by `num_attention_heads`.
        """
        super().__init__(config=config)
        self.dtype = dtype
        self.param_dtype = param_dtype
        self.precision = precision
        self.rngs = rngs

        self.hidden_size = config.hidden_size
        self.head_dim = self.config.hidden_size // self.config.num_attention_heads
        self.num_key_value_groups = self.config.num_attention_heads // self.config.num_key_value_heads

        if self.num_key_value_groups == 1:
            assert self.config.num_attention_heads == self.config.num_key_value_heads
        linear_class = partial(
            ParallelLinear,
            dtype=dtype,
            param_dtype=param_dtype,
            kernel_init=jax.nn.initializers.normal(config.initializer_range),
            precision=precision,
            **get_dot_general_by_bits(config.bits, config.easy_method),
        )
        self.q_proj = linear_class(
            config.hidden_size,
            config.num_attention_heads * self.head_dim,
            rngs=rngs,
            use_bias=True,
        )
        self.k_proj = linear_class(
            config.hidden_size,
            config.num_key_value_heads * self.head_dim,
            rngs=rngs,
            use_bias=True,
        )
        self.v_proj = linear_class(
            config.hidden_size,
            config.num_key_value_heads * self.head_dim,
            rngs=rngs,
            use_bias=True,
        )
        self.o_proj = linear_class(
            config.num_attention_heads * self.head_dim,
            config.hidden_size,
            rngs=rngs,
            use_bias=False,
        )

        self.sliding_window = config.sliding_window if config.layer_types[layer_idx] == "sliding_attention" else None

        self.attention_performer = FlexibleAttentionModule(
            rngs=rngs,
            base_config=config,
            softmax_scale=self.head_dim**-0.5,
            dropout_prob=config.attention_dropout,
        )
        self.resid_dropout = nn.Dropout(rate=config.resid_pdrop)
        self.rotary = self.config.get_basic_rope(
            head_size=config.hidden_size // config.num_attention_heads,
            rotary_dim=config.hidden_size // config.num_attention_heads,
            base=config.rope_theta,
            dtype=self.dtype,
        )

    def __call__(
        self,
        hidden_states: chex.Array,
        attention_mask: chex.Array,
        position_ids: chex.Array,
        causal_mask: chex.Array | bool | None,
        mode: common_types.RUNTIME_MODE_TYPES,  # type:ignore
        cache_view: TransformerCacheView | PagesCacheView | None = None,
        cache_metadata: TransformerMetadata | PagesMetadata | None = None,
        segment_ids: chex.Array | None = None,
        output_attentions: bool = False,
        fcm_mask: chex.Array | None = None,
        frequencies: chex.Array | None = None,
    ):
        """
        Forward pass of the Qwen2Attention module.

        Args:
            hidden_states (chex.Array): Input hidden states.
            attention_mask (chex.Array): Mask to apply on the attention scores.
            position_ids (chex.Array): Position indices for the tokens. Shape: (batch_size, sequence_length).
            causal_mask (tp.Optional[chex.Array | bool]): Causal mask for ensuring autoregressive behavior.
            cache_view (tp.Optional[TransformerCacheView | PagesCacheView]): Cache view for attention KVs.
            cache_metadata (tp.Optional[TransformerMetadata | PagesMetadata]): Metadata for paged attention.
            segment_ids (tp.Optional[chex.Array]): Segment IDs for segment-based attention (optional).
            output_attentions (bool): Whether to return attention weights. Default is False.
            fcm_mask (tp.Optional[chex.Array]): Flash Chunking Mask (FCM) for attention.
            frequencies (tp.Optional[chex.Array]): Precomputed rotary frequency embeddings.

        Returns:
            tp.Union[tp.Tuple[chex.Array, chex.Array], tp.Tuple[chex.Array]]:
                A tuple containing the attention output hidden states. If `output_attentions` is True,
                it also includes the attention weights.
        """
        batch_size, sequence_length = hidden_states.shape[:2]
        query_states, key_states, value_states = (
            self.q_proj(hidden_states),
            self.k_proj(hidden_states),
            self.v_proj(hidden_states),
        )

        query_states = query_states.reshape(batch_size, sequence_length, self.config.num_attention_heads, self.head_dim)
        key_states = key_states.reshape(batch_size, sequence_length, self.config.num_key_value_heads, self.head_dim)
        value_states = value_states.reshape(batch_size, sequence_length, self.config.num_key_value_heads, self.head_dim)
        query_states, key_states, value_states = self.apply_qkv_shardings(query_states, key_states, value_states)

        query_states, key_states = self.rotary(
            positions=position_ids,
            query=query_states,
            key=key_states,
            frequencies=frequencies,
        )

        (
            key_states,
            value_states,
            attention_mask,
            init_attention_bias,
            cache_view,
            cache_metadata,
        ) = self.concatenate(
            query=query_states,
            key=key_states,
            value=value_states,
            cache_view=cache_view,
            cache_metadata=cache_metadata,
            attention_mask=attention_mask,
            causal_mask=causal_mask,
            fcm_mask=fcm_mask,
            sliding_window=None,  # FORCE DISABLE SLIDING WINDOW TO FIX SLICE ERROR
        )

        attentions = self.attention_performer.forward(
            query_states=query_states,
            key_states=key_states,
            value_states=value_states,
            mode=mode,
            bias=None,
            cache_metadata=cache_metadata,
            cache_view=cache_view,
            init_bias=init_attention_bias,
            attention_mask=attention_mask,
            segment_ids=segment_ids,
            causal=True,
            sliding_window=None,  # FORCE DISABLE SLIDING WINDOW TO FIX SLICE ERROR
        )

        attn_output = self.shard_attention_prod(self._merge_heads(attentions.attention_outputs))
        attn_output = self.o_proj(attn_output)

        attn_output = self.resid_dropout(attn_output)
        return AttentionLayerOutput(
            attention_output=attn_output,
            attention_weight=attentions.attention_weights if output_attentions else None,
            cache_view=cache_view,
        )


class Qwen2DecoderLayer(nn.Module):
    """Qwen2 Transformer Decoder Layer.

    This module represents a single decoder layer in the Qwen2 model,
    combining self-attention and MLP sub-layers with residual connections
    and RMS normalization.

    Attributes:
        config (Qwen2Config): Configuration object for the model.
        dtype (jnp.dtype): Data type for computations.
        param_dtype (jnp.dtype): Data type for parameters.
        precision (jax.lax.PrecisionLike): Precision setting for JAX operations.
        rngs (nn.Rngs): Random number generators.
        input_layernorm (RMSNorm): RMS normalization applied before the attention layer.
        self_attn (Qwen2Attention): The self-attention module.
        mlp (Qwen2MLP): The feed-forward (MLP) module.
        post_attention_layernorm (RMSNorm): RMS normalization applied after the attention layer and before the MLP layer.
    """

    def __init__(
        self,
        config: Qwen2Config,
        layer_idx: int,
        dtype: jnp.dtype = jnp.bfloat16,
        param_dtype: jnp.dtype = jnp.bfloat16,
        precision: jax.lax.PrecisionLike = None,
        *,
        rngs: nn.Rngs,
    ):
        """Initializes the Qwen2DecoderLayer.

        Args:
            config (Qwen2Config): The configuration object for the Qwen2 model.
            dtype (jnp.dtype): Data type for computation. Defaults to jnp.float32.
            param_dtype (jnp.dtype): Data type for parameters. Defaults to jnp.float32.
            precision (jax.lax.PrecisionLike): Precision setting for JAX operations. Defaults to None.
            rngs (nn.Rngs): Random number generators.
        """
        self.config = config
        self.dtype = dtype
        self.param_dtype = param_dtype
        self.precision = precision
        attn_block = Qwen2Attention
        mlp_block = Qwen2MLP
        attn_block, mlp_block = auto_remat(
            attn_block,
            mlp_block,
            policy=config.gradient_checkpointing,
        )

        self.self_attn = attn_block(
            config=config,
            layer_idx=layer_idx,
            dtype=dtype,
            param_dtype=param_dtype,
            precision=precision,
            rngs=rngs,
        )

        self.mlp = mlp_block(
            config=config,
            dtype=dtype,
            param_dtype=param_dtype,
            precision=precision,
            rngs=rngs,
        )
        self.input_layernorm = RMSNorm(
            config.hidden_size,
            eps=self.config.rms_norm_eps,
            dtype=dtype,
            param_dtype=param_dtype,
            rngs=rngs,
        )
        self.post_attention_layernorm = RMSNorm(
            config.hidden_size,
            eps=self.config.rms_norm_eps,
            dtype=dtype,
            param_dtype=param_dtype,
            rngs=rngs,
        )

    def __call__(
        self,
        hidden_states: chex.Array,
        attention_mask: chex.Array,
        position_ids: chex.Array,
        causal_mask: chex.Array | bool | None,
        mode: common_types.RUNTIME_MODE_TYPES,  # type:ignore
        cache_view: TransformerCacheView | PagesCacheView | None = None,
        cache_metadata: TransformerMetadata | PagesMetadata | None = None,
        segment_ids: chex.Array | None = None,
        output_attentions: bool = False,
        fcm_mask: chex.Array | None = None,
        frequencies: chex.Array | None = None,
    ):
        """Forward pass of the Qwen2DecoderLayer module.

        Args:
            hidden_states (chex.Array): Input hidden states.
            attention_mask (chex.Array): Mask to apply on the attention scores.
            position_ids (chex.Array): Position indices for the tokens. Shape: (batch_size, sequence_length).
            causal_mask (tp.Optional[chex.Array | bool]): Causal mask for ensuring autoregressive behavior.
            cache_view (tp.Optional[TransformerCacheView | PagesCacheView]): Cache view for attention KVs.
            cache_metadata (tp.Optional[TransformerMetadata | PagesMetadata]): Metadata for paged attention.
            segment_ids (tp.Optional[chex.Array]): Segment IDs for segment-based attention (optional).
            output_attentions (bool): Whether to return attention weights. Default is False.
            fcm_mask (tp.Optional[chex.Array]): Flash Chunking Mask (FCM) for attention.
            frequencies (tp.Optional[chex.Array]): Precomputed rotary frequency embeddings.

        Returns:
            tp.Tuple[chex.Array, tp.Optional[chex.Array]]:
                A tuple containing the output hidden states and optionally the attention weights.
        """

        attn_outputs = self.self_attn(
            self.input_layernorm(hidden_states),
            attention_mask,
            position_ids,
            causal_mask,
            mode,
            cache_view,
            cache_metadata,
            segment_ids,
            output_attentions,
            fcm_mask,
            frequencies,
        )
        hidden_states = hidden_states + attn_outputs.attention_output

        feed_forward_input = self.post_attention_layernorm(hidden_states)

        if self.config.use_scan_mlp:
            feed_forward_hidden_states = block_wise_ffn(
                self.mlp,
                feed_forward_input,
                self.config.scan_mlp_chunk_size,
            )
        else:
            feed_forward_hidden_states = self.mlp(
                feed_forward_input,
            )

        hidden_states = hidden_states + feed_forward_hidden_states
        hidden_states = apply_logical_sharding(
            hidden_states,
            dynamic_axes=common_types.HiddenStateSharding,
            partition_manager=self.config.partition_manager,
        )
        return DecoderLayerOutput(
            hidden_states=hidden_states,
            attention_weight=attn_outputs.attention_weight,
            cache_view=attn_outputs.cache_view,
        )


@register_module(TaskType.BASE_MODULE, config=Qwen2Config, model_type="qwen2")
class Qwen2Model(EasyDeLBaseModule):
    """The base Qwen2 model transformer.

    This class represents the core transformer architecture of the Qwen2 model,
    consisting of an embedding layer, multiple Qwen2DecoderLayer layers,
    and a final RMS normalization layer.

    Attributes:
        config (Qwen2Config): Configuration object for the model.
        dtype (jnp.dtype): Data type for computation.
        param_dtype (jnp.dtype): Data type for parameters.
        precision (jax.lax.PrecisionLike): Precision setting for JAX operations.
        rngs (nn.Rngs): Random number generators.
        embed_tokens (nn.Embed): Embedding layer for input tokens.
        layers (tp.List[Qwen2DecoderLayer]): List of decoder layers.
        norm (RMSNorm): Final layer normalization.
        dropout (nn.Dropout): Dropout layer applied after embeddings.
        gradient_checkpointing (EasyDeLGradientCheckPointers): Gradient checkpointing configuration.
    """

    def __init__(
        self,
        config: Qwen2Config,
        dtype: jnp.dtype = jnp.bfloat16,
        param_dtype: jnp.dtype = jnp.bfloat16,
        precision: jax.lax.PrecisionLike = None,
        *,
        rngs: nn.Rngs,
    ):
        """Initializes the Qwen2Model.

        Args:
            config (Qwen2Config): The configuration object for the Qwen2 model.
            dtype (jnp.dtype): Data type for computation. Defaults to jnp.float32.
            param_dtype (jnp.dtype): Data type for parameters. Defaults to jnp.float32.
            precision (jax.lax.PrecisionLike): Precision setting for JAX operations. Defaults to None.
            rngs (nn.Rngs): Random number generators.
        """
        super().__init__(
            config=config,
            dtype=dtype,
            param_dtype=param_dtype,
            precision=precision,
            rngs=rngs,
        )
        self.embed_tokens = nn.Embed(
            config.vocab_size,
            config.hidden_size,
            embedding_init=jax.nn.initializers.normal(stddev=config.initializer_range),
            dtype=dtype,
            param_dtype=param_dtype,
            rngs=rngs,
        )
        self.dropout = nn.Dropout(rate=config.embd_pdrop)
        self.layers = [
            Qwen2DecoderLayer(
                config=config,
                layer_idx=i,
                dtype=dtype,
                param_dtype=param_dtype,
                precision=precision,
                rngs=rngs,
            )
            for i in range(config.num_hidden_layers)
        ]
        self.norm = RMSNorm(
            config.hidden_size,
            eps=config.rms_norm_eps,
            dtype=dtype,
            param_dtype=param_dtype,
            rngs=rngs,
        )

    def __call__(
        self,
        input_ids: chex.Array | None = None,
        inputs_embeds: chex.Array | None = None,
        attention_mask: chex.Array | None = None,
        position_ids: chex.Array | None = None,
        segment_ids: chex.Array | None = None,
        output_attentions: bool | None = None,
        output_hidden_states: bool | None = None,
        mode: common_types.RUNTIME_MODE_TYPES | None = None,  # type:ignore
        past_key_values: TransformerCache | PagesCache | None = None,
        cache_metadata: TransformerMetadata | PagesMetadata | None = None,
    ) -> BaseModelOutput:
        """Forward pass of the Qwen2Model.

        Args:
            input_ids (tp.Optional[chex.Array]): Input token IDs. Shape: (batch_size, sequence_length).
            inputs_embeds (tp.Optional[chex.Array]): Input embeddings.
                Either `input_ids` or `inputs_embeds` must be provided.
            attention_mask (tp.Optional[chex.Array]): Mask to avoid performing attention on padding token indices.
                Shape: (batch_size, sequence_length).
            position_ids (tp.Optional[chex.Array]): Position indices for the tokens.
                Shape: (batch_size, sequence_length).
            segment_ids (tp.Optional[chex.Array]): Segment IDs (unused).
            output_attentions (tp.Optional[bool]): Whether to return attention weights.
                Defaults to `config.output_attentions`.
            output_hidden_states (tp.Optional[bool]): Whether to return hidden states for all layers.
                Defaults to `config.output_hidden_states`.
            past_key_values (tp.Optional[TransformerCache | PagesCache]):
                Precomputed key/value states for attention.
            cache_metadata (tp.Optional[TransformerMetadata | PagesMetadata]): Metadata for paged attention.

        Returns:
            BaseModelOutput: The model's output.
                returns a `BaseModelOutput` object containing `last_hidden_state`, `hidden_states` (optional),
                and `attentions` (optional).

        Raises:
            ValueError: If neither `input_ids` nor `inputs_embeds` is provided.
        """
        if (input_ids is None) ^ (inputs_embeds is not None):
            raise ValueError(
                "You cannot specify both input_ids and inputs_embeds at the same time, and must specify either one"
            )
        if inputs_embeds is None:
            inputs_embeds = self.embed_tokens(input_ids.astype("i4"))
        batch_size, sequence_length, _ = inputs_embeds.shape

        all_attentions = () if output_attentions else None
        all_hidden_states = () if output_hidden_states else None
        assert sequence_length <= self.config.max_position_embeddings, (
            f"Maximum Position Embedding Reached ! "
            f"(Excepted <= {self.config.max_position_embeddings} got {sequence_length})"
        )
        if attention_mask is None:
            attention_mask = jnp.ones((batch_size, sequence_length), "b1")
        else:
            if attention_mask.dtype != jnp.bool:
                attention_mask = jnp.astype(attention_mask == 1, "b1")
        if position_ids is None:
            position_ids = jnp.broadcast_to(
                jnp.clip(jnp.cumsum(attention_mask, axis=-1) - 1, a_min=0),
                (batch_size, sequence_length),
            ).astype(jnp.int32)

        hidden_states = self.dropout(inputs_embeds)
        if mode is None:
            mode = (
                common_types.MODE_DECODE
                if sequence_length == 1 and past_key_values is not None
                else common_types.MODE_TRAIN
            )
        if past_key_values is None:
            past_key_values = TransformerCache.init_empty(len(self.layers))

        hidden_states = apply_logical_sharding(
            hidden_states,
            dynamic_axes=common_types.HiddenStateSharding,
            partition_manager=self.config.partition_manager,
        )
        for idx, block in enumerate(self.layers):
            if output_hidden_states:
                all_hidden_states += (hidden_states,)

            layer_outputs = block(
                hidden_states=hidden_states,
                attention_mask=attention_mask,
                position_ids=position_ids,
                mode=mode,
                cache_view=past_key_values.views[idx],
                cache_metadata=cache_metadata,
                causal_mask=self.causal_mask,
                output_attentions=output_attentions,
                segment_ids=segment_ids,
                frequencies=self.frequencies,
            )
            hidden_states = layer_outputs.hidden_states

            if output_attentions:
                all_attentions += (layer_outputs.attention_weight,)

            past_key_values[idx] = layer_outputs.cache_view

        hidden_states = self.norm(hidden_states)

        if output_hidden_states:
            all_hidden_states += (hidden_states,)

        return BaseModelOutput(
            last_hidden_state=hidden_states,
            hidden_states=all_hidden_states,
            attentions=all_attentions,
            past_key_values=past_key_values,
        )


@register_module(TaskType.CAUSAL_LM, config=Qwen2Config, model_type="qwen2")
class Qwen2ForCausalLM(EasyDeLBaseModule):
    """Qwen2 model with a Causal Language Modeling head.

    This model consists of the base Qwen2 transformer (`Qwen2Model`) followed by a
    linear layer (`lm_head`) that projects the transformer's output hidden states
    to the vocabulary size, producing logits for next token prediction.
    Optionally, the input token embeddings can be tied to the output projection layer.

    Attributes:
        config (Qwen2Config): Configuration object for the model.
        dtype (jnp.dtype): Data type for computation.
        param_dtype (jnp.dtype): Data type for parameters.
        precision (jax.lax.PrecisionLike): Precision setting for JAX operations.
        rngs (nn.Rngs): Random number generators.
        model (Qwen2Model): The core Qwen2 transformer model.
        lm_head (ParallelLinear): The linear layer for projecting hidden states to vocabulary logits.
    """

    def __init__(
        self,
        config: Qwen2Config,
        dtype: jnp.dtype = jnp.bfloat16,
        param_dtype: jnp.dtype = jnp.bfloat16,
        precision: jax.lax.PrecisionLike = None,
        *,
        rngs: nn.Rngs,
    ):
        """Initializes the Qwen2ForCausalLM model.

        Args:
            config (Qwen2Config): The configuration object for the Qwen2 model.
            dtype (jnp.dtype): Data type for computation. Defaults to jnp.float32.
            param_dtype (jnp.dtype): Data type for parameters. Defaults to jnp.float32.
            precision (jax.lax.PrecisionLike): Precision setting for JAX operations. Defaults to None.
            rngs (nn.Rngs): Random number generators.
        """
        super().__init__(
            config=config,
            dtype=dtype,
            param_dtype=param_dtype,
            precision=precision,
            rngs=rngs,
        )
        self.model = Qwen2Model(
            config=config,
            dtype=dtype,
            param_dtype=param_dtype,
            precision=precision,
            rngs=rngs,
        )

        self.lm_head = ParallelLinear(
            config.hidden_size,
            config.vocab_size,
            dtype=dtype,
            param_dtype=param_dtype,
            rngs=rngs,
            use_bias=False,
            kernel_init=jax.nn.initializers.normal(stddev=config.initializer_range),
            precision=precision,
            **get_dot_general_by_bits(config.bits, config.easy_method),
        )

    def __call__(
        self,
        input_ids: chex.Array | None = None,
        inputs_embeds: chex.Array | None = None,
        attention_mask: chex.Array | None = None,
        position_ids: chex.Array | None = None,
        segment_ids: chex.Array | None = None,
        output_attentions: bool | None = None,
        output_hidden_states: bool | None = None,
        mode: common_types.RUNTIME_MODE_TYPES | None = None,  # type:ignore
        past_key_values: TransformerCache | PagesCache | None = None,
        cache_metadata: TransformerMetadata | PagesMetadata | None = None,
    ) -> CausalLMOutput:
        """Forward pass of the Qwen2ForCausalLM model.

        Args:
            input_ids (tp.Optional[chex.Array]): Input token IDs. Shape: (batch_size, sequence_length).
            inputs_embeds (tp.Optional[chex.Array]): Input embeddings.
                Either `input_ids` or `inputs_embeds` must be provided.
            attention_mask (tp.Optional[chex.Array]): Mask to avoid performing attention on padding token indices.
                Shape: (batch_size, sequence_length).
            position_ids (tp.Optional[chex.Array]): Position indices for the tokens.
                Shape: (batch_size, sequence_length).
            segment_ids (tp.Optional[chex.Array]): Segment IDs (unused).
            output_attentions (tp.Optional[bool]): Whether to return attention weights.
                Defaults to `config.output_attentions`.
            output_hidden_states (tp.Optional[bool]): Whether to return hidden states for all layers.
                Defaults to `config.output_hidden_states`.
            past_key_values (tp.Optional[TransformerCache | PagesCache]):
                Precomputed key/value states for attention.
            cache_metadata (tp.Optional[TransformerMetadata | PagesMetadata]): Metadata for paged attention.


        Returns:
            CausalLMOutput: The model's output.
                returns a `CausalLMOutput` object containing `logits`, `hidden_states` (optional),
                and `attentions` (optional).
        """
        outputs = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            mode=mode,
            past_key_values=past_key_values,
            cache_metadata=cache_metadata,
            inputs_embeds=inputs_embeds,
            segment_ids=segment_ids,
        )

        hidden_states = outputs.last_hidden_state

        hidden_states = apply_logical_sharding(
            hidden_states,
            dynamic_axes=common_types.HiddenStateSharding,
            partition_manager=self.config.partition_manager,
        )

        if self.config.tie_word_embeddings:
            lm_logits = jax.lax.dot_general(
                hidden_states,
                self.model.embed_tokens.embedding.value.T,
                (((hidden_states.ndim - 1), (0,)), ((), ())),
            )
        else:
            lm_logits = self.lm_head(hidden_states)

        return CausalLMOutput(
            logits=lm_logits,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
            past_key_values=outputs.past_key_values,
        )


@register_module(TaskType.SEQUENCE_CLASSIFICATION, config=Qwen2Config, model_type="qwen2")
class Qwen2ForSequenceClassification(EasyDeLBaseModule):
    """Qwen2 model with a Sequence Classification head.

    This model consists of the base Qwen2 transformer (`Qwen2Model`) followed by a
    linear layer (`score`) that projects the transformer's output hidden states
    (typically the hidden state of the last token or a pooled representation) to
        the number of classes for classification.

    Attributes:
        config (Qwen2Config): Configuration object for the model.
        dtype (jnp.dtype): Data type for computation.
        param_dtype (jnp.dtype): Data type for parameters.
        precision (jax.lax.PrecisionLike): Precision setting for JAX operations.
        rngs (nn.Rngs): Random number generators.
        model (Qwen2Model): The core Qwen2 transformer model.
        score (ParallelLinear): The linear layer for classification.
    """

    def __init__(
        self,
        config: Qwen2Config,
        dtype: jnp.dtype = jnp.bfloat16,
        param_dtype: jnp.dtype = jnp.bfloat16,
        precision: jax.lax.PrecisionLike = None,
        *,
        rngs: nn.Rngs,
    ):
        """Initializes the Qwen2ForSequenceClassification model.

        Args:
            config (Qwen2Config): The configuration object for the Qwen2 model.
                Must include `num_labels`.
            dtype (jnp.dtype): Data type for computation. Defaults to jnp.float32.
            param_dtype (jnp.dtype): Data type for parameters. Defaults to jnp.float32.
            precision (jax.lax.PrecisionLike): Precision setting for JAX operations. Defaults to None.
            rngs (nn.Rngs): Random number generators.

        Raises:
            AssertionError: If `config.num_labels` is not defined.
        """
        super().__init__(
            config=config,
            dtype=dtype,
            param_dtype=param_dtype,
            precision=precision,
            rngs=rngs,
        )
        self.model = Qwen2Model(
            config=config,
            dtype=dtype,
            param_dtype=param_dtype,
            precision=precision,
            rngs=rngs,
        )
        assert hasattr(config, "num_labels"), (
            "in order to use `SequenceClassification` Models in `EasyDeL` "
            "you first need to attach `num_labels` to model `config`"
        )
        self.score = ParallelLinear(
            self.config.hidden_size,
            config.num_labels,
            dtype=dtype,
            param_dtype=param_dtype,
            use_bias=False,
            kernel_init=jax.nn.initializers.normal(stddev=config.initializer_range),
            precision=self.precision,
            rngs=rngs,
        )

    def __call__(
        self,
        input_ids: chex.Array | None = None,
        inputs_embeds: chex.Array | None = None,
        attention_mask: chex.Array | None = None,
        position_ids: chex.Array | None = None,
        segment_ids: chex.Array | None = None,
        mode: common_types.RUNTIME_MODE_TYPES | None = None,  # type:ignore
        past_key_values: TransformerCache | PagesCache | None = None,
        cache_metadata: TransformerMetadata | PagesMetadata | None = None,
        output_attentions: bool | None = None,
        output_hidden_states: bool | None = None,
    ) -> SequenceClassifierOutput:
        """Forward pass of the Qwen2ForSequenceClassification model.

        Args:
            input_ids (tp.Optional[chex.Array]): Input token IDs. Shape: (batch_size, sequence_length).
            inputs_embeds (tp.Optional[chex.Array]): Input embeddings.
                Either `input_ids` or `inputs_embeds` must be provided.
            attention_mask (tp.Optional[chex.Array]): Mask to avoid performing attention on padding token indices.
                Shape: (batch_size, sequence_length).
            position_ids (tp.Optional[chex.Array]): Position indices for the tokens.
                Shape: (batch_size, sequence_length).
            segment_ids (tp.Optional[chex.Array]): Segment IDs (unused).
            past_key_values (tp.Optional[TransformerCache | PagesCache]):
                Precomputed key/value states for attention.
            cache_metadata (tp.Optional[TransformerMetadata | PagesMetadata]): Metadata for paged attention.
            output_attentions (tp.Optional[bool]): Whether to return attention weights.
                Defaults to `config.output_attentions`.
            output_hidden_states (tp.Optional[bool]): Whether to return hidden states for all layers.
                Defaults to `config.output_hidden_states`.


        Returns:
            SequenceClassifierOutput: The model's output,
                returns a `SequenceClassifierOutput` object containing `logits`, `hidden_states` (optional),
                and `attentions` (optional).

        Raises:
            ValueError: If `config.pad_token_id` is None and `batch_size > 1`.
        """
        transformer_outputs = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            mode=mode,
            past_key_values=past_key_values,
            cache_metadata=cache_metadata,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            inputs_embeds=inputs_embeds,
            segment_ids=segment_ids,
        )

        hidden_states = transformer_outputs.last_hidden_state
        logits = self.score(hidden_states)
        if input_ids is not None:
            batch_size = input_ids.shape[0]
        else:
            batch_size = inputs_embeds.shape[0]

        if self.config.pad_token_id is None and batch_size != 1:
            raise ValueError("Cannot handle batch sizes > 1 if no padding token is defined.")
        if self.config.pad_token_id is None:
            sequence_lengths = -1
        else:
            if input_ids is not None:
                sequence_lengths = jnp.argmax(jnp.equal(input_ids, self.config.pad_token_id).astype("i4"), -1) - 1
                sequence_lengths = sequence_lengths % input_ids.shape[-1]
            else:
                sequence_lengths = -1

        pooled_logits = logits[jnp.arange(batch_size), sequence_lengths]

        return SequenceClassifierOutput(
            logits=pooled_logits,
            past_key_values=past_key_values,
            hidden_states=transformer_outputs.hidden_states,
            attentions=transformer_outputs.attentions,
        )
