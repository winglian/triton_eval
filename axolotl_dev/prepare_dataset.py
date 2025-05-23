import weave

from datasets import load_dataset, DatasetDict
from dataclasses import dataclass
import simple_parsing as sp

@dataclass
class Args:
    dataset_name: str = "tcapelle/train_ds_triton_v2f2"#"GPUMODE/Inductor_Created_Data_Permissive"
    prepared_dataset_name: str = "tcapelle/train_ds_triton"
    code_column: str = "pt_code_without_tests"
    split: str = "train"


SYSTEM_PROMPT = """
You are an expert in Triton programming, capable of writing corresponding Triton kernels and wrapper functions based on functional descriptions and function parameters. 

# Instructions
- Ensure that the wrapper function fully corresponds to the provided function information.
- Generate a detailed plan on how to convert and optimize the Pytorch code to a Triton kernel before writing the code.
- The reasoning process MUST BE enclosed within <think> and </think> tags."
- Reply with the thinking process and a single blob of code surrounded with ```python and ```.
"""

USER_PROMPT = """Convert the following PyTorch code to a Triton kernel.
Pytorch code:
```python
{pytorch_code}```

The function should have the same name as the PyTorch function: {entrypoint}

Don't forget to format your answer as:
<think>
thinking process
</think>

```python
import torch

# relevant imports
from typing import ...

def {entrypoint}(...):
    ...
```"""


def get_dataset(dataset_name, code_column, split="train"):
    # Load the dataset - this is expected to be preprocessed by prepare_dataset.py
    data = load_dataset(dataset_name)[split]
    print(f"Loaded {len(data)} examples from {dataset_name} split {split}")
    data = data.filter(lambda x: x[code_column] is not None)
    print(f"Filtered to {len(data)} examples with non-None {code_column}")
    data = data.filter(lambda x: x["entrypoint"] is not None)
    print(f"Filtered to {len(data)} examples with non-None entrypoint")
    data = data.filter(lambda x: x["tests"] is not None)
    print(f"Filtered to {len(data)} examples with non-None tests")
    data = data.filter(lambda x: x["pt_code_runs"])
    print(f"Filtered to {len(data)} examples with non-None pt_code_runs")

    # Format the prompt with the preprocessed code
    def format_example(example):
        # Format the prompt with the preprocessed code
        pytorch_code = example[code_column]
        entrypoint = example["entrypoint"]
        return {
            "prompt": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": USER_PROMPT.format(pytorch_code=pytorch_code, entrypoint=entrypoint)},
                {"role": "assistant", "content": "<think>\n"},
            ],
        }
    
    # Format each example in the dataset
    formatted_data = data.map(format_example)

    return formatted_data

if __name__ == "__main__":
    args = sp.parse(Args)
    dataset = get_dataset(args.dataset_name, args.code_column, split=args.split)
    dataset_dict = DatasetDict({
        "train": dataset,
        "debug": dataset.select(range(24)),
    })

    dataset_dict.push_to_hub(args.prepared_dataset_name, commit_message="push prepared")

    weave.init("grpo-cuda/axolotl-grpo")
    wds = weave.Dataset(rows=dataset.to_list(), name="train_ds_triton")
    weave.publish(wds)
