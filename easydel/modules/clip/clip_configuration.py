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

import typing

from eformer.common_types import ColumnWise, Replicated, RowWise

from easydel.infra.base_module import EasyDeLBaseConfig
from easydel.infra.factory import register_config
from easydel.utils.helpers import get_logger

logger = get_logger(__name__)


def _get_partition_rules(self, *arg, **kwargs):
    """Generic partition rules for CLIP text and vision models.

    Args:
            self: The configuration object (unused but part of method signature).
            *arg: Additional positional arguments (unused).
            **kwargs: Additional keyword arguments (unused).

    Returns:
            Tuple: A tuple of partition rules for model parameters.
    """
    pmag = self.partition_manager  # Handles resolving strategies
    return (
        # 1. Text Embeddings
        (r"text_model/embeddings/token_embedding/embedding", pmag.resolve(ColumnWise)),
        (r"text_model/embeddings/position_embedding/embedding", pmag.resolve(ColumnWise)),
        (r"vision_model/embeddings/class_embedding", pmag.resolve(Replicated)),
        (r"vision_model/embeddings/patch_embedding/kernel", pmag.resolve(ColumnWise)),
        (r"vision_model/embeddings/patch_embedding/bias", pmag.resolve(Replicated)),
        (r"vision_model/embeddings/position_embedding/embedding", pmag.resolve(ColumnWise)),
        (
            r"(text|vision)_model/encoder/layers/\d+/self_attn/(q_proj|k_proj|v_proj)/kernel",
            pmag.resolve(ColumnWise),
        ),
        (
            r"(text|vision)_model/encoder/layers/\d+/self_attn/out_proj/kernel",
            pmag.resolve(RowWise),
        ),
        (
            r"(text|vision)_model/encoder/layers/\d+/self_attn/.*proj/bias",
            pmag.resolve(Replicated),
        ),
        (
            r"(text|vision)_model/encoder/layers/\d+/mlp/fc1/kernel",
            pmag.resolve(ColumnWise),
        ),
        (r"(text|vision)_model/encoder/layers/\d+/mlp/fc2/kernel", pmag.resolve(RowWise)),
        (
            r"(text|vision)_model/encoder/layers/\d+/mlp/fc(1|2)/bias",
            pmag.resolve(Replicated),
        ),
        (r".*norm.*/scale", pmag.resolve(Replicated)),
        (r".*norm.*/bias", pmag.resolve(Replicated)),
        (r"(visual|text)_projection/kernel", pmag.resolve(ColumnWise)),
        (r"(visual|text)_projection/bias", pmag.resolve(Replicated)),
        (r"logit_scale", pmag.resolve(Replicated)),
        (r"classifier/kernel", pmag.resolve(RowWise)),
        (r"classifier/bias", pmag.resolve(Replicated)),
        (r".*bias", pmag.resolve(Replicated)),
        (r".*", pmag.resolve(Replicated)),
    )


@register_config("clip_text_model")
class CLIPTextConfig(EasyDeLBaseConfig):
    r"""
    This is the configuration class to store the configuration of a [`CLIPTextModel`]. It is used to instantiate a CLIP
    text encoder according to the specified arguments, defining the model architecture. Instantiating a configuration
    with the defaults will yield a similar configuration to that of the text encoder of the CLIP
    [openai/clip-vit-base-patch32](https://huggingface.co/openai/clip-vit-base-patch32) architecture.

    Configuration objects inherit from [`EasyDeLBaseConfig`] and can be used to control the model outputs. Read the
    documentation from [`EasyDeLBaseConfig`] for more information.

    Args:
            vocab_size (`int`, *optional*, defaults to 49408):
                    Vocabulary size of the CLIP text model. Defines the number of different tokens that can be
                    represented by the `inputs_ids` passed when calling [`CLIPModel`].
            hidden_size (`int`, *optional*, defaults to 512):
                    Dimensionality of the encoder layers and the pooler layer.
            intermediate_size (`int`, *optional*, defaults to 2048):
                    Dimensionality of the "intermediate" (i.e., feed-forward) layer in the Transformer encoder.
            projection_dim (`int`, *optional*, defaults to 512):
                    Dimensionality of text and vision projection layers.
            num_hidden_layers (`int`, *optional*, defaults to 12):
                    Number of hidden layers in the Transformer encoder.
            num_attention_heads (`int`, *optional*, defaults to 8):
                    Number of attention heads for each attention layer in the Transformer encoder.
            max_position_embeddings (`int`, *optional*, defaults to 77):
                    The maximum sequence length that this model might ever be used with. Typically set this to something
                large just in case (e.g., 512 or 1024 or 2048).
            hidden_act (`str` or `function`, *optional*, defaults to `"quick_gelu"`):
                    The non-linear activation function (function or string) in the encoder and pooler. If string,
                    `"gelu"`,`"relu"`, `"selu"` and `"gelu_new"` `"quick_gelu"` are supported.
            layer_norm_eps (`float`, *optional*, defaults to 1e-05):
                    The epsilon used by the layer normalization layers.
            attention_dropout (`float`, *optional*, defaults to 0.0):
                    The dropout ratio for the attention probabilities.
            initializer_range (`float`, *optional*, defaults to 0.02):
                    The standard deviation of the truncated_normal_initializer for initializing all weight matrices.
            initializer_factor (`float`, *optional*, defaults to 1.0):
                    A factor for initializing all weight matrices
                    (should be kept to 1, used internally for initialization testing).
            pad_token_id (`int`, *optional*, defaults to 1):
                    Padding token id.
            bos_token_id (`int`, *optional*, defaults to 49406):
                    Beginning of stream token id.
            eos_token_id (`int`, *optional*, defaults to 49407):
                    End of stream token id.

    Example:

    ```python
    >>> from transformers import CLIPTextConfig, CLIPTextModel

    >>> # Initializing a CLIPTextConfig with openai/clip-vit-base-patch32 style configuration
    >>> configuration = CLIPTextConfig()

    >>> # Initializing a CLIPTextModel (with random weights) from the openai/clip-vit-base-patch32 style configuration
    >>> model = CLIPTextModel(configuration)

    >>> # Accessing the model configuration
    >>> configuration = model.config
    ```"""

    model_type = "clip_text_model"
    base_config_key = "text_config"

    def __init__(
        self,
        vocab_size=49408,
        hidden_size=512,
        intermediate_size=2048,
        projection_dim=512,
        num_hidden_layers=12,
        num_attention_heads=8,
        max_position_embeddings=77,
        hidden_act="quick_gelu",
        layer_norm_eps=1e-5,
        attention_dropout=0.0,
        initializer_range=0.02,
        initializer_factor=1.0,
        pad_token_id=1,
        bos_token_id=49406,
        eos_token_id=49407,
        **kwargs,
    ):
        super().__init__(
            pad_token_id=pad_token_id,
            bos_token_id=bos_token_id,
            eos_token_id=eos_token_id,
            **kwargs,
        )

        self.vocab_size = vocab_size
        self.hidden_size = hidden_size
        self.intermediate_size = intermediate_size
        self.projection_dim = projection_dim
        self.num_hidden_layers = num_hidden_layers
        self.num_attention_heads = num_attention_heads
        self.max_position_embeddings = max_position_embeddings
        self.layer_norm_eps = layer_norm_eps
        self.hidden_act = hidden_act
        self.initializer_range = initializer_range
        self.initializer_factor = initializer_factor
        self.attention_dropout = attention_dropout

    get_partition_rules = _get_partition_rules


@register_config("clip_vision_model")
class CLIPVisionConfig(EasyDeLBaseConfig):
    r"""
    This is the configuration class to store the configuration of a [`CLIPVisionModel`]. It is used to instantiate a
    CLIP vision encoder according to the specified arguments, defining the model architecture. Instantiating a
    configuration with the defaults will yield a similar configuration to that of the vision encoder of the CLIP
    [openai/clip-vit-base-patch32](https://huggingface.co/openai/clip-vit-base-patch32) architecture.

    Configuration objects inherit from [`EasyDeLBaseConfig`] and can be used to control the model outputs. Read the
    documentation from [`EasyDeLBaseConfig`] for more information.

    Args:
            hidden_size (`int`, *optional*, defaults to 768):
                    Dimensionality of the encoder layers and the pooler layer.
            intermediate_size (`int`, *optional*, defaults to 3072):
                    Dimensionality of the "intermediate" (i.e., feed-forward) layer in the Transformer encoder.
            projection_dim (`int`, *optional*, defaults to 512):
                    Dimensionality of text and vision projection layers.
            num_hidden_layers (`int`, *optional*, defaults to 12):
                    Number of hidden layers in the Transformer encoder.
            num_attention_heads (`int`, *optional*, defaults to 12):
                    Number of attention heads for each attention layer in the Transformer encoder.
            num_channels (`int`, *optional*, defaults to 3):
                    The number of input channels.
            image_size (`int`, *optional*, defaults to 224):
                    The size (resolution) of each image.
            patch_size (`int`, *optional*, defaults to 32):
                    The size (resolution) of each patch.
            hidden_act (`str` or `function`, *optional*, defaults to `"quick_gelu"`):
                    The non-linear activation function (function or string) in the encoder and pooler.
                    If string, `"gelu"`, `"relu"`, `"selu"` and `"gelu_new"` `"quick_gelu"` are supported.
            layer_norm_eps (`float`, *optional*, defaults to 1e-05):
                    The epsilon used by the layer normalization layers.
            attention_dropout (`float`, *optional*, defaults to 0.0):
                    The dropout ratio for the attention probabilities.
            initializer_range (`float`, *optional*, defaults to 0.02):
                    The standard deviation of the truncated_normal_initializer for initializing all weight matrices.
            initializer_factor (`float`, *optional*, defaults to 1.0):
                    A factor for initializing all weight matrices (should be kept to 1, used internally
                    for initialization testing).

    Example:

    ```python
    >>> from transformers import CLIPVisionConfig, CLIPVisionModel

    >>> # Initializing a CLIPVisionConfig with openai/clip-vit-base-patch32 style configuration
    >>> configuration = CLIPVisionConfig()

    >>> # Initializing a CLIPVisionModel (with random weights) from the openai/clip-vit-base-patch32 style configuration
    >>> model = CLIPVisionModel(configuration)

    >>> # Accessing the model configuration
    >>> configuration = model.config
    ```"""

    model_type = "clip_vision_model"
    base_config_key = "vision_config"

    def __init__(
        self,
        hidden_size=768,
        intermediate_size=3072,
        projection_dim=512,
        num_hidden_layers=12,
        num_attention_heads=12,
        num_channels=3,
        image_size=224,
        patch_size=32,
        hidden_act="quick_gelu",
        layer_norm_eps=1e-5,
        attention_dropout=0.0,
        initializer_range=0.02,
        initializer_factor=1.0,
        **kwargs,
    ):
        super().__init__(**kwargs)

        self.hidden_size = hidden_size
        self.intermediate_size = intermediate_size
        self.projection_dim = projection_dim
        self.num_hidden_layers = num_hidden_layers
        self.num_attention_heads = num_attention_heads
        self.num_channels = num_channels
        self.patch_size = patch_size
        self.image_size = image_size
        self.initializer_range = initializer_range
        self.initializer_factor = initializer_factor
        self.attention_dropout = attention_dropout
        self.layer_norm_eps = layer_norm_eps
        self.hidden_act = hidden_act

    get_partition_rules = _get_partition_rules


@register_config("clip")
class CLIPConfig(EasyDeLBaseConfig):
    r"""
    [`CLIPConfig`] is the configuration class to store the configuration of a [`CLIPModel`]. It is used to instantiate
    a CLIP model according to the specified arguments, defining the text model and vision model configs. Instantiating
    a configuration with the defaults will yield a similar configuration to that of the CLIP
    [openai/clip-vit-base-patch32](https://huggingface.co/openai/clip-vit-base-patch32) architecture.

    Configuration objects inherit from [`EasyDeLBaseConfig`] and can be used to control the model outputs. Read the
    documentation from [`EasyDeLBaseConfig`] for more information.

    Args:
            text_config (`dict`, *optional*):
                    Dictionary of configuration options used to initialize [`CLIPTextConfig`].
            vision_config (`dict`, *optional*):
                    Dictionary of configuration options used to initialize [`CLIPVisionConfig`].
            projection_dim (`int`, *optional*, defaults to 512):
                    Dimensionality of text and vision projection layers.
            logit_scale_init_value (`float`, *optional*, defaults to 2.6592):
                    The initial value of the *logit_scale* parameter. Default is used as per the
                    original CLIP implementation.
            kwargs (*optional*):
                    Dictionary of keyword arguments.

    Example:

    ```python
    >>> from transformers import CLIPConfig, CLIPModel

    >>> # Initializing a CLIPConfig with openai/clip-vit-base-patch32 style configuration
    >>> configuration = CLIPConfig()

    >>> # Initializing a CLIPModel (with random weights) from the openai/clip-vit-base-patch32 style configuration
    >>> model = CLIPModel(configuration)

    >>> # Accessing the model configuration
    >>> configuration = model.config

    >>> # We can also initialize a CLIPConfig from a CLIPTextConfig and a CLIPVisionConfig
    >>> from transformers import CLIPTextConfig, CLIPVisionConfig

    >>> # Initializing a CLIPText and CLIPVision configuration
    >>> config_text = CLIPTextConfig()
    >>> config_vision = CLIPVisionConfig()

    >>> config = CLIPConfig.from_text_vision_configs(config_text, config_vision)
    ```"""

    model_type = "clip"
    sub_configs: typing.ClassVar = {"text_config": CLIPTextConfig, "vision_config": CLIPVisionConfig}

    def __init__(
        self,
        text_config=None,
        vision_config=None,
        projection_dim=512,
        logit_scale_init_value=2.6592,
        **kwargs,
    ):
        text_config_dict = kwargs.pop("text_config_dict", None)
        vision_config_dict = kwargs.pop("vision_config_dict", None)

        super().__init__(**kwargs)

        if text_config_dict is not None:
            if text_config is None:
                text_config = {}

            _text_config_dict = CLIPTextConfig(**text_config_dict).to_dict()
            for key, value in _text_config_dict.items():
                if key in text_config and value != text_config[key] and key not in ["transformers_version"]:
                    if key in text_config_dict:
                        message = (
                            f"`{key}` is found in both `text_config_dict` and `text_config` but with different values. "
                            f'The value `text_config_dict["{key}"]` will be used instead.'
                        )
                    else:
                        message = (
                            f"`text_config_dict` is provided which will be used to initialize `CLIPTextConfig`. The "
                            f'value `text_config["{key}"]` will be overridden.'
                        )
                    logger.info(message)

            # Update all values in `text_config` with the ones in `_text_config_dict`.
            text_config.update(_text_config_dict)

        if vision_config_dict is not None:
            if vision_config is None:
                vision_config = {}

            # This is the complete result when using `vision_config_dict`.
            _vision_config_dict = CLIPVisionConfig(**vision_config_dict).to_dict()
            # convert keys to string instead of integer
            if "id2label" in _vision_config_dict:
                _vision_config_dict["id2label"] = {
                    str(key): value for key, value in _vision_config_dict["id2label"].items()
                }

            # Give a warning if the values exist in both `_vision_config_dict` and `vision_config` but being different.
            for key, value in _vision_config_dict.items():
                if key in vision_config and value != vision_config[key] and key not in ["transformers_version"]:
                    # If specified in `vision_config_dict`
                    if key in vision_config_dict:
                        message = (
                            f"`{key}` is found in both `vision_config_dict` and `vision_config` but with different "
                            f'values. The value `vision_config_dict["{key}"]` will be used instead.'
                        )
                    # If inferred from default argument values (just to be super careful)
                    else:
                        message = (
                            f"`vision_config_dict` is provided which will be used to initialize `CLIPVisionConfig`. "
                            f'The value `vision_config["{key}"]` will be overridden.'
                        )
                    logger.info(message)

            # Update all values in `vision_config` with the ones in `_vision_config_dict`.
            vision_config.update(_vision_config_dict)

        if text_config is None:
            text_config = {}
            logger.info("`text_config` is `None`. Initializing the `CLIPTextConfig` with default values.")

        if vision_config is None:
            vision_config = {}
            logger.info("`vision_config` is `None`. initializing the `CLIPVisionConfig` with default values.")

        self.text_config = CLIPTextConfig(**text_config)
        self.vision_config = CLIPVisionConfig(**vision_config)

        self.projection_dim = projection_dim
        self.logit_scale_init_value = logit_scale_init_value
        self.initializer_factor = 1.0

    @classmethod
    def from_text_vision_configs(cls, text_config: CLIPTextConfig, vision_config: CLIPVisionConfig, **kwargs):
        r"""
        Instantiate a [`CLIPConfig`] (or a derived class) from clip text model configuration and clip vision model
        configuration.

        Args:
                text_config (CLIPTextConfig): The text model configuration.
                vision_config (CLIPVisionConfig): The vision model configuration.
                **kwargs: Additional keyword arguments.

        Returns:
                [`CLIPConfig`]: An instance of a configuration object
        """

        return cls(
            text_config=text_config.to_dict(),
            vision_config=vision_config.to_dict(),
            **kwargs,
        )

    get_partition_rules = _get_partition_rules
