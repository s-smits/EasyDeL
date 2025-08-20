import re
from dataclasses import field

import jax
from datasets import Dataset, load_dataset
from eformer.pytree import auto_pytree
from jax import numpy as jnp
from transformers import AutoConfig, AutoTokenizer

import easydel as ed
from easydel.infra.factory import registry
from easydel.modules import *  # noqa # init


@auto_pytree
class RunTimeConfig:
    """
    Configuration class for runtime settings.

    Attributes:
        repo_id (str): The repository ID.
        processor_repo_id (str, optional): The repository ID for the processor. If None, defaults to repo_id.
        refrence_model_repo_id (str, optional): The repository ID for the reference model. If None, defaults to repo_id.
        sharding_axis (Tuple[int]): The sharding axis. Defaults to (1, -1, 1, 1, 1).
        attn_mechanism (ed.AttentionMechanisms): The attention mechanism to use. Defaults to
            ed.AttentionMechanisms.VANILLA.
        param_dtype (jnp.dtype): The data type for model parameters. Defaults to jnp.bfloat16.
        dtype (jnp.dtype): The data type for general computation. Defaults to jnp.bfloat16.
        attn_dtype (jnp.dtype): The data type for attention computation. Defaults to jnp.bfloat16.
        attn_softmax_dtype (jnp.dtype): The data type for attention softmax computation. Defaults to jnp.float32.
    """

    repo_id: str = field(
        metadata={"help": "The repository ID."},
    )

    processor_repo_id: str | None = field(
        default=None,
        metadata={"help": "The repository ID for the processor. If None, defaults to repo_id."},
    )
    xml_reward: float = field(default=0.125)
    xml_full_match_reward: float = field(default=0.5)
    xml_full_match_reject: float = field(default=0.0)
    correctness_reward: float = field(default=2.0)
    kv_cache_quantization: ed.EasyDeLQuantizationMethods = field(default=ed.EasyDeLQuantizationMethods.NONE)

    sharding_axis: str = field(
        default="1, -1, 1, 1, 1",
        metadata={"help": "The sharding axis."},
    )
    attn_mechanism: ed.AttentionMechanisms = field(
        default=ed.AttentionMechanisms.AUTO, metadata={"help": "The attention mechanism to use."}
    )

    param_dtype: jnp.dtype = field(default=jnp.bfloat16, metadata={"help": "The data type for model parameters."})
    dtype: jnp.dtype = field(default=jnp.bfloat16, metadata={"help": "The data type for general computation."})
    attn_dtype: jnp.dtype = field(default=jnp.bfloat16, metadata={"help": "The data type for attention computation."})
    attn_softmax_dtype: jnp.dtype = field(
        default=jnp.float32, metadata={"help": "The data type for attention softmax computation."}
    )

    def __post_init__(self):
        """Post-initialization to set dependent parameters."""
        if self.processor_repo_id is None:
            self.processor_repo_id = self.repo_id
        if isinstance(self.sharding_axis, str):
            self.sharding_axis = tuple(map(int, self.sharding_axis.split(",")))


parser = ed.utils.DataClassArgumentParser((ed.GRPOConfig, RunTimeConfig))
grpo_config, runtime_config = parser.parse_args_into_dataclasses()

runtime_config: RunTimeConfig
grpo_config: ed.GRPOConfig

if jax.process_index() == 0:
    print("Training Arguments\n----------------------")
    print(grpo_config)
    print("----------------------")


def main():
    processor = AutoTokenizer.from_pretrained(runtime_config.processor_repo_id)

    if processor.pad_token_id is None:
        processor.pad_token_id = processor.eos_token_id

    hf_config = AutoConfig.from_pretrained(runtime_config.repo_id)

    avails = [v.module.__name__ for v in registry.task_registry[ed.TaskType.IMAGE_TEXT_TO_TEXT].values()]

    if hf_config.architectures and any(arch in avails for arch in hf_config.architectures):
        load_module = ed.AutoEasyDeLModelForImageTextToText
    else:
        load_module = ed.AutoEasyDeLModelForCausalLM

    model = load_module.from_pretrained(
        runtime_config.repo_id,
        auto_shard_model=True,
        sharding_axis_dims=runtime_config.sharding_axis,
        config_kwargs=ed.EasyDeLBaseConfigDict(
            freq_max_position_embeddings=grpo_config.max_sequence_length,
            mask_max_position_embeddings=grpo_config.max_sequence_length,
            attn_dtype=runtime_config.attn_dtype,
            attn_softmax_dtype=runtime_config.attn_softmax_dtype,
            kv_cache_quantization_method=runtime_config.kv_cache_quantization,
            attn_mechanism=runtime_config.attn_mechanism,
            gradient_checkpointing=ed.EasyDeLGradientCheckPointers.NONE,  # change this if u go OOM
        ),
        quantization_method=ed.EasyDeLQuantizationMethods.NONE,
        param_dtype=runtime_config.param_dtype,
        dtype=runtime_config.dtype,
        precision=jax.lax.Precision.DEFAULT,
        partition_axis=ed.PartitionAxis(),
    )

    SYSTEM_PROMPT = """
	Respond in the following format:
	<think>
	...
	</think>
	<answer>
	...
	</answer>
	"""

    def extract_xml_answer(text: str) -> str:
        answer = text.split("<answer>")[-1]
        answer = answer.split("</answer>")[0]
        return answer.strip()

    def extract_hash_answer(text: str):
        if "####" not in text:
            return None
        return text.split("####")[1].strip()

    def get_gsm8k_questions(split="train") -> Dataset:
        data = load_dataset("openai/gsm8k", "main")[split]
        data = data.map(
            lambda x: {
                "prompt": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": x["question"]},
                ],
                "answer": extract_hash_answer(x["answer"]),
            }
        )
        return data

    def correctness_reward_func(prompts, completions, batch, **kwargs) -> list[float]:
        responses = [completion[0]["content"] for completion in completions]
        extracted_responses = [extract_xml_answer(r) for r in responses]
        answer = processor.batch_decode(batch["answer_ids"]) * grpo_config.num_return_sequences
        return [
            runtime_config.correctness_reward if r == a else 0.0
            for r, a in zip(extracted_responses, answer, strict=False)
        ]

    def int_reward_func(completions, **kwargs) -> list[float]:
        responses = [completion[0]["content"] for completion in completions]
        extracted_responses = [extract_xml_answer(r) for r in responses]
        return [0.5 if r.isdigit() else 0.0 for r in extracted_responses]

    def strict_format_reward_func(completions, **kwargs) -> list[float]:
        """Reward function that checks if the completion has a specific format."""
        pattern = r"^<think>\n.*?\n</think>\n<answer>\n.*?\n</answer>\n$"
        responses = [completion[0]["content"] for completion in completions]
        matches = [re.match(pattern, r) for r in responses]
        return [
            runtime_config.xml_full_match_reward if match else runtime_config.xml_full_match_reject for match in matches
        ]

    def soft_format_reward_func(completions, **kwargs) -> list[float]:
        """Reward function that checks if the completion has a specific format."""
        pattern = r"<think>.*?</think>\s*<answer>.*?</answer>"
        responses = [completion[0]["content"] for completion in completions]
        matches = [re.match(pattern, r) for r in responses]
        return [
            runtime_config.xml_full_match_reward if match else runtime_config.xml_full_match_reject for match in matches
        ]

    def count_xml(text) -> float:
        count = 0.0
        if text.count("<think>\n") == 1:
            count += runtime_config.xml_reward
        if text.count("\n</think>\n") == 1:
            count += runtime_config.xml_reward
        if text.count("\n<answer>\n") == 1:
            count += runtime_config.xml_reward
            count -= len(text.split("\n</answer>\n")[-1]) * 0.001
        if text.count("\n</answer>") == 1:
            count += runtime_config.xml_reward
            count -= (len(text.split("\n</answer>")[-1]) - 1) * 0.001
        return count

    def xmlcount_reward_func(completions, **kwargs) -> list[float]:
        contents = [completion[0]["content"] for completion in completions]
        return [count_xml(c) for c in contents]

    train_dataset = get_gsm8k_questions("train")
    test_dataset = get_gsm8k_questions("test")

    def data_tokenize_fn(batch, tokenizer, tools):
        ids = tokenizer(
            batch["prompt"],
            return_tensors="np",
            padding="max_length",
            padding_side="left",
            max_length=grpo_config.max_prompt_length,
            truncation=True,
            add_special_tokens=False,
        )
        ans = tokenizer(
            batch["answer"],
            return_tensors="np",
            padding="max_length",
            padding_side="left",
            max_length=grpo_config.max_prompt_length,
            truncation=True,
            add_special_tokens=False,
            return_attention_mask=False,
        )
        ids.update({"answer_ids": ans["input_ids"]})
        return ids

    trainer = ed.GRPOTrainer(
        model=model,
        reward_funcs=[
            xmlcount_reward_func,
            soft_format_reward_func,
            strict_format_reward_func,
            int_reward_func,
            correctness_reward_func,
        ],
        processing_class=processor,
        eval_dataset=test_dataset,
        train_dataset=train_dataset,
        arguments=grpo_config,
        data_tokenize_fn=data_tokenize_fn,
    )

    trainer.train()


if __name__ == "__main__":
    main()
