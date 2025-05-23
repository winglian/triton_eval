import yaml
import random  
import weave
import wandb
import math
import subprocess
import tempfile
import re
import torch
import os
import httpx
import asyncio
import torch.distributed as dist
import logging
from contextlib import nullcontext
from tools import extract_code, run_python_code  # run_python_in_process no longer used
from kernel_checks import uses_torch_in_kernel, count_primitives

# Configure httpx logger to only show WARNING or higher levels
logging.getLogger("httpx").setLevel(logging.WARNING)

try:
    if dist.is_initialized() and dist.get_rank() == 0:
        weave.init("grpo-cuda/axolotl-grpo")
except:
    # If not in distributed mode or any other error, initialize weave anyway
    weave.init("grpo-cuda/axolotl-grpo")

RUN_ON_SERVER = True  # When True, execute Triton code via the /run_code API instead of locally

SERVER_URL = os.environ.get("TRITON_SERVER_URL", "http://127.0.0.1:9347")
RUN_CODE_ENDPOINT = f"{SERVER_URL}/run_code"

def get_wandb_run():
    if wandb.run is None:
        return None
    return wandb.run

def wandb_attributes():
    "Add the wandb metrics as weave attributes"
    if wandb.run is None:
        return nullcontext()
    else:
        run = wandb.run
        wandb_metrics = {k: v for k, v in dict(run.summary).items() if not k.startswith("_")}
        return weave.attributes(wandb_metrics)

async def _run_code_on_server(code: str, tests: str) -> dict:
    """Synchronously execute Triton `code` + `tests` on the remote worker pool.

    Returns a dict compatible with the structure produced by `run_python_code`:
    `{"stdout": str, "stderr": str, "status_code": int}`
    """
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(RUN_CODE_ENDPOINT,
                                     json={"code": code, "tests": tests},
                                     timeout=180.0)
            resp.raise_for_status()
            data = resp.json()
            return data
        except httpx.HTTPStatusError as e:
            # 503, 504, 500… – treat as execution failure
            return {"stdout": "", "stderr": str(e), "status_code": -1}
        except Exception as e:
            # Network or other unexpected error
            return {"stdout": "", "stderr": str(e), "status_code": -1}

# generated deadlocks with tokenizers
# weave.init("grpo-cuda/axolotl-grpo")

AVAILABLE_GPUS = list(range(torch.cuda.device_count()))

# ===== Reward Magnitudes =====
# Centralized dictionary for reward/penalty values
REWARD_MAGNITUDES = {
    "think_ok": 0.1,
    "think_not_ok": -0.1,
    "one_code_blob_ok": 0.1,
    "one_code_blob_not_ok": -0.1,
    "code_runs_fail": -0.2,
    "code_runs_incorrect": 0.0,
    "code_runs_correct": 1.0,
    "imports_decorator_ok": 0.1,
    "constexpr_ok": 0.1,
    "valid_tl_methods_ok": 0.1,
    "masks_load_store_ok": 0.1,
    "torch_empty_penalty": -0.1,
    "torch_zeros_ok": 0.1,
    "exp_penalty": 0.5,
}

try: # this is not working, we are not saving the reward magnitudes to wandb
    if dist.is_initialized() and dist.get_rank() == 0:
        if wandb.run is not None:
            # we want to savbe the Reward magnitudes to wandb
            # let's dump them to a temp file first, yaml
            temp_file = tempfile.NamedTemporaryFile(delete=False)
            yaml.dump(REWARD_MAGNITUDES, temp_file)
            wandb.save(temp_file.name)
            wandb.config.update({"reward_magnitudes": REWARD_MAGNITUDES})
except:
    pass




# List of valid triton.language methods
# Sourced from triton.__dir__() and filtering for relevant functions
tl_methods = [
    'PropagateNan', 'TRITON_MAX_TENSOR_NUMEL', 'abs', 'advance', 'arange',
    'argmax', 'argmin', 'associative_scan', 'atomic_add', 'atomic_and',
    'atomic_cas', 'atomic_max', 'atomic_min', 'atomic_or', 'atomic_xchg',
    'atomic_xor', 'bfloat16', 'block_type', 'broadcast', 'broadcast_to',
    'cast', 'cat', 'cdiv', 'ceil', 'clamp', 'const', 'const_pointer_type',
    'constexpr', 'cos', 'cumprod', 'cumsum', 'debug_barrier', 'device_assert',
    'device_print', 'div_rn', 'dot', 'dtype', 'erf', 'exp', 'exp2',
    'expand_dims', 'fdiv', 'flip', 'float16', 'float32', 'float64',
    'float8e4b15', 'float8e4b8', 'float8e4nv', 'float8e5', 'float8e5b16',
    'floor', 'fma', 'full', 'function_type', 'histogram',
    'inline_asm_elementwise', 'int1', 'int16', 'int32', 'int64', 'int8',
    'interleave', 'join', 'load', 'log', 'log2', 'make_block_ptr', 'max',
    'max_constancy', 'max_contiguous', 'maximum', 'min', 'minimum',
    'multiple_of', 'num_programs', 'pair_uniform_to_normal', 'permute',
    'philox', 'pi32_t', 'pointer_type', 'program_id', 'rand', 'rand4x',
    'randint', 'randint4x', 'randn', 'randn4x', 'range', 'ravel', 'reduce',
    'reshape', 'rsqrt', 'sigmoid', 'sin', 'softmax', 'sort', 'split', 'sqrt',
    'sqrt_rn', 'static_assert', 'static_print', 'static_range', 'store',
    'str_to_ty', 'sum', 'swizzle2d', 'tensor', 'trans', 'uint16', 'uint32',
    'uint64', 'uint8', 'uint_to_uniform_float', 'umulhi', 'view', 'void',
    'where', 'xor_sum', 'zeros', 'zeros_like'
]
VALID_TL_METHODS = set(tl_methods)

# ===== Reward Functions =====
@weave.op
def think_scorer(output):
    "Check if the output has exactly one <think> block with content >= 100 chars"
    thinking_content = re.findall(r"<think>(.*?)</think>", output, re.DOTALL)

    num_matches = len(thinking_content)
    thinking_ok = False
    thinking_length = 0

    if num_matches == 1:
        content_length = len(thinking_content[0].strip())
        thinking_length = content_length
        if content_length >= 100:
            thinking_ok = True

    return {"thinking_ok": thinking_ok, "thinking_length": thinking_length}

def think_reward(completions, **kwargs):
    "Reward the model for having a thinking process"
    responses = [completion[0]['content'] for completion in completions]
    rewards = []
    for response in responses:
        with wandb_attributes():
            think_score = think_scorer(response)
            ok = think_score["thinking_ok"]
            thinking_length = think_score["thinking_length"]
            thinking_length = max(thinking_length - 5000, 0)
        reward = REWARD_MAGNITUDES["think_ok"] * math.exp(-REWARD_MAGNITUDES["exp_penalty"]*thinking_length/1000) if ok else REWARD_MAGNITUDES["think_not_ok"]
        rewards.append(reward)
    return rewards

@weave.op
def one_code_blob(output):
    "Check if the output has exactly one Python code blob after removing the think block"
    # Remove the think block first
    output_without_think = output.split("</think>")[-1].strip()

    code_blobs = re.findall(r"```python(.*?)```", output_without_think, re.DOTALL)

    num_matches = len(code_blobs)
    one_code_blob_ok = False
    code_length = 0

    if num_matches == 1:
        content = code_blobs[0].strip()
        code_length = len(content)
        if code_length > 0:
            one_code_blob_ok = True

    return {"one_code_blob_ok": one_code_blob_ok, "code_length": code_length}

def one_code_blob_reward(completions, **kwargs):
    "Reward the model for having a single code blob after the think block"
    responses = [completion[0]['content'] for completion in completions]
    rewards = []
    for response in responses:
        # The scorer now handles removing the think block internally
        with wandb_attributes():
            code_blob_score = one_code_blob(response)
        code_length = code_blob_score["code_length"]
        code_length = max(code_length - 5000, 0)
        ok = code_blob_score["one_code_blob_ok"]
        # Penalize more heavily if no code blob found after removing think
        reward = REWARD_MAGNITUDES["one_code_blob_ok"] * math.exp(-REWARD_MAGNITUDES["exp_penalty"]*code_length/1000) if ok else REWARD_MAGNITUDES["one_code_blob_not_ok"]
        rewards.append(reward)
    return rewards

@weave.op
def is_code_hacking(triton_code: str) -> dict:
    """
    Static analysis: detect torch.* inside Triton kernels and count primitives.
    Returns dict with keys 'hacked' and 'primitive_count'.
    """
    hacked = uses_torch_in_kernel(triton_code)
    primitive_count = count_primitives(triton_code)
    return {"hacked": hacked, "primitive_count": primitive_count}

@weave.op
async def run_scorer_async(output: str, tests: str, pytorch_code_output: str):
    "Runs the code and returns the output"
    assert isinstance(tests, str), f"tests is not a string: {tests}"
    # run pt code
    gpu_id = random.choice(AVAILABLE_GPUS)
    env = {
        "CUDA_VISIBLE_DEVICES": str(gpu_id),
    }

    triton_code = extract_code(output)
    # static hack & coverage analysis
    analysis = is_code_hacking(triton_code)
    if analysis["hacked"]:
        # hacked kernels short-circuit
        return {"triton_runs": False, "correct": False,
                "hacked": True, "primitive_count": analysis["primitive_count"]}
    # too-short output => no kernel
    if len(triton_code) < 10:
        return {"triton_runs": False, "correct": False,
                "primitive_count": analysis["primitive_count"]}
    triton_and_test = f'import torch\n{triton_code}\n\n{"#"*146}\n\n{tests}'

    if RUN_ON_SERVER:
        triton_output = await _run_code_on_server(triton_code, tests)
    else:
        # Fallback to local execution (kept for completeness)
        try:
            triton_output = run_python_code(triton_and_test, env)
        except subprocess.TimeoutExpired:
            return {"triton_runs": False, "correct": False,
                    "primitive_count": analysis["primitive_count"]}

    runs = triton_output["status_code"] == 0
    correct = pytorch_code_output == triton_output["stdout"] and runs

    result = {
        "triton_runs": runs,
        "correct": correct
        }
    
    # attach dynamic result and coverage
    triton_output.update(result)
    triton_output["primitive_count"] = analysis["primitive_count"]
    return triton_output

def _compute_code_runs_reward(run_output):
    "If the code doesn't run, return -1, if it runs but isn't correct, return 0, otherwise return 1"
    triton_runs = run_output["triton_runs"]
    correct = run_output["correct"]
    if not triton_runs:
        return REWARD_MAGNITUDES["code_runs_fail"]
    elif not correct:
        return REWARD_MAGNITUDES["code_runs_incorrect"]
    else:
        return REWARD_MAGNITUDES["code_runs_correct"]

@weave.op
def reward_code_runs(completions, tests, stdout, **kwargs):
    """Synchronous wrapper around the async implementation."""
    
    async def _compute_async():
        responses = [completion[0]['content'] for completion in completions]
        # delegate to hack-gated dynamic run
        tasks = [run_scorer_async(resp, test, pt_std)
                 for resp, test, pt_std in zip(responses, tests, stdout)]
        with wandb_attributes():
            run_scores = await asyncio.gather(*tasks)
        return [_compute_code_runs_reward(score) for score in run_scores]

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_compute_async())
    else:
        return loop.run_until_complete(_compute_async())

# ===== Static Code Analysis Rewards =====

@weave.op
def imports_decorator_scorer(code: str) -> dict:
    """Checks for required imports and @triton.jit decorator."""
    has_triton_import = "import triton" in code
    has_tl_import = "import triton.language as tl" in code
    has_jit_decorator = "@triton.jit" in code or "@tl.jit" in code # Allow both forms
    all_present = has_triton_import and has_tl_import and has_jit_decorator
    return {
        "has_triton_import": has_triton_import,
        "has_tl_import": has_tl_import,
        "has_jit_decorator": has_jit_decorator,
        "imports_decorator_ok": all_present,
    }

def imports_decorator_reward(completions, **kwargs):
    """Rewards 0.2 if required imports and decorator are present, 0 otherwise."""
    responses = [completion[0]['content'] for completion in completions]
    rewards = []
    for response in responses:
        triton_code = extract_code(response)
        if not triton_code:
            rewards.append(0) # Penalize lack of code elsewhere
            continue
        with wandb_attributes():
            score = imports_decorator_scorer(triton_code)
        rewards.append(REWARD_MAGNITUDES["imports_decorator_ok"] if score["imports_decorator_ok"] else 0)
    return rewards

@weave.op
def constexpr_scorer(code: str) -> dict:
    """Checks if tl.constexpr is used."""
    uses_constexpr = re.search(r"tl\.constexpr[,\s]+", code) is not None
    return {"uses_constexpr": uses_constexpr}

def constexpr_reward(completions, **kwargs):
    """Rewards 0.2 if tl.constexpr is used, 0 otherwise."""
    responses = [completion[0]['content'] for completion in completions]
    rewards = []
    for response in responses:
        triton_code = extract_code(response)
        if not triton_code:
            rewards.append(0)
            continue
        with wandb_attributes():
            score = constexpr_scorer(triton_code)
        rewards.append(REWARD_MAGNITUDES["constexpr_ok"] if score["uses_constexpr"] else 0)
    return rewards

@weave.op
def valid_tl_methods_scorer(code: str) -> dict:
    """Checks if only valid triton.language methods are used."""
    # Find all occurrences of tl.<method_name>
    # This regex looks for 'tl.' followed by a valid Python identifier
    # We capture the identifier name
    used_methods = set(re.findall(r"tl\.([a-zA-Z_]\w*)", code))

    invalid_methods_used = used_methods - VALID_TL_METHODS
    all_valid = len(invalid_methods_used) == 0

    return {
        "all_tl_methods_valid": all_valid,
        "invalid_methods_found": list(invalid_methods_used) # For debugging
    }

def valid_tl_methods_reward(completions, **kwargs):
    """Rewards 0.2 if only valid tl methods are used, 0 otherwise."""
    responses = [completion[0]['content'] for completion in completions]
    rewards = []
    for response in responses:
        triton_code = extract_code(response)
        if not triton_code:
            rewards.append(0)
            continue
        with wandb_attributes():
            score = valid_tl_methods_scorer(triton_code)
        rewards.append(REWARD_MAGNITUDES["valid_tl_methods_ok"] if score["all_tl_methods_valid"] else 0)
    return rewards

@weave.op
def masks_load_store_scorer(code: str) -> dict:
    """Checks if masks are used during load and store operations."""
    # Check for tl.load(..., mask=...) or tl.store(..., mask=...)
    # This regex is simplified and might need refinement for complex cases
    uses_mask_load = re.search(r"tl\.load\s*\(.*mask\s*=", code, re.DOTALL) is not None
    uses_mask_store = re.search(r"tl\.store\s*\(.*mask\s*=", code, re.DOTALL) is not None

    # Require mask for at least one load or store if they exist
    has_load = "tl.load" in code
    has_store = "tl.store" in code

    uses_mask = False
    if has_load and uses_mask_load:
        uses_mask = True
    if has_store and uses_mask_store:
         uses_mask = True
    # If there are no loads or stores, this check doesn't apply negatively
    if not has_load and not has_store:
        uses_mask = True # Or maybe 0 reward? Let's say True for now.

    return {"uses_mask_load_store": uses_mask}

def masks_load_store_reward(completions, **kwargs):
    """Rewards 0.1 if masks are used with tl.load/tl.store, 0 otherwise."""
    responses = [completion[0]['content'] for completion in completions]
    rewards = []
    for response in responses:
        triton_code = extract_code(response)
        if not triton_code:
            rewards.append(0)
            continue
        with wandb_attributes():
            score = masks_load_store_scorer(triton_code)
        rewards.append(REWARD_MAGNITUDES["masks_load_store_ok"] if score["uses_mask_load_store"] else 0)
    return rewards

@weave.op
def torch_empty_scorer(code: str) -> dict:
    """Checks if torch.empty is used."""
    uses_torch_empty = re.search(r"torch\.empty\s*\(", code) is not None
    return {"uses_torch_empty": uses_torch_empty}

def torch_empty_penalty(completions, **kwargs):
    """Penalizes -0.1 if torch.empty is used, 0 otherwise."""
    responses = [completion[0]['content'] for completion in completions]
    rewards = []
    for response in responses:
        triton_code = extract_code(response)
        if not triton_code:
            rewards.append(0)
            continue
        # Check only within the kernel function definition if possible?
        # For now, check the whole extracted code blob.
        with wandb_attributes():
            score = torch_empty_scorer(triton_code)
        rewards.append(REWARD_MAGNITUDES["torch_empty_penalty"] if score["uses_torch_empty"] else 0)
    return rewards

@weave.op
def torch_zeros_scorer(code: str) -> dict:
    """Checks if torch.zeros is used."""
    uses_torch_zeros = re.search(r"torch\.zeros\s*\(", code) is not None
    return {"uses_torch_zeros": uses_torch_zeros}

def torch_zeros_reward(completions, **kwargs):
    """Rewards 0.1 if torch.zeros is used, 0 otherwise."""
    responses = [completion[0]['content'] for completion in completions]
    rewards = []
    for response in responses:
        triton_code = extract_code(response)
        if not triton_code:
            rewards.append(0)
            continue
        # Similar to torch.empty, ideally check only in entrypoint.
        with wandb_attributes():
            score = torch_zeros_scorer(triton_code)
        rewards.append(REWARD_MAGNITUDES["torch_zeros_ok"] if score["uses_torch_zeros"] else 0)
    return rewards
