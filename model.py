# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
from dataclasses import dataclass
from collections import defaultdict
from typing import Optional, Dict, Any

import math
import torch
import torch.nn as nn
from torch import Tensor
from torch.nn import functional as F

from attention_utils import scaled_dot_product_attention
from cache import get_cache_constructor
from prompt_compression import get_prompt_compressor_constructor


def find_multiple(n: int, k: int) -> int:
    if n % k == 0:
        return n
    return n + k - (n % k)


@dataclass
class ModelArgs:
    block_size: int = 4000
    vocab_size: int = 32000
    n_layer: int = 32
    n_head: int = 32
    dim: int = 4096
    intermediate_size: int = None
    n_local_heads: int = -1
    head_dim: int = 64
    rope_base: float = 10000
    norm_eps: float = 1e-5
    attention_bias: bool = False
    max_length: int = 4096
    rope_scaling: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if self.n_local_heads == -1:
            self.n_local_heads = self.n_head
        if self.intermediate_size is None:
            hidden_dim = 4 * self.dim
            n_hidden = int(2 * hidden_dim / 3)
            self.intermediate_size = find_multiple(n_hidden, 256)
        self.head_dim = self.dim // self.n_head

    @classmethod
    def from_name(cls, name: str):
        if name in transformer_configs:
            return cls(**transformer_configs[name])
        # fuzzy search
        config = [
            config
            for config in transformer_configs
            if config in str(name).upper() or config in str(name)
        ]

        # We may have two or more configs matched (e.g. "7B" and "Mistral-7B"). Find the best config match,
        # take longer name (as it have more symbols matched)
        if len(config) > 1:
            config.sort(key=len, reverse=True)
            assert len(config[0]) != len(
                config[1]
            ), name  # make sure only one 'best' match

        return cls(**transformer_configs[config[0]])


# block_size --> max context length
# vocab_size --> size of vocab/token set
# n_layer --> number of transformers layer
# dim --> embedding dim
# rope_base --> base value for RopE position embeddings
transformer_configs = {
    "CodeLlama-7b-Python-hf": dict(block_size=16384, vocab_size=32000, n_layer=32, dim=4096, rope_base=1000000),
    "LLaMA-2-7B-32K": dict(block_size=32000, vocab_size=32000, n_layer=32, dim=4096),
    "7B": dict(n_layer=32, n_head=32, dim=4096),
    "13B": dict(n_layer=40, n_head=40, dim=5120),
    "30B": dict(n_layer=60, n_head=52, dim=6656),
    "34B": dict(
        n_layer=48,
        n_head=64,
        dim=8192,
        vocab_size=32000,
        n_local_heads=8,
        intermediate_size=22016,
        rope_base=1000000,
    ),  # CodeLlama-34B-Python-hf
    "70B": dict(
        n_layer=80, n_head=64, dim=8192, n_local_heads=8, intermediate_size=28672
    ),
    "Mistral-7B": dict(
        n_layer=32,
        n_head=32,
        n_local_heads=8, # quest for group-query attention
        dim=4096,
        intermediate_size=14336,
        vocab_size=32000,
    ),
    "stories15M": dict(n_layer=6, n_head=6, dim=288),
    "stories110M": dict(n_layer=12, n_head=12, dim=768),
    "Meta-Llama-3-8B-Instruct": dict(
        block_size=8192,
        n_layer=32,
        n_head=32,
        n_local_heads=8,
        dim=4096,
        intermediate_size=14336,
        vocab_size=128256,
        rope_base=500000,
        max_length=8192,
    ),
    "Meta-Llama-3.1-8B-Instruct": dict(
        block_size=131072,
        n_layer=32,
        n_head=32,
        n_local_heads=8,
        dim=4096,
        intermediate_size=14336,
        vocab_size=128256,
        rope_base=500000,
        max_length=131072,
        rope_scaling={
            "factor": 8.0,
            "low_freq_factor": 1.0,
            "high_freq_factor": 4.0,
            "original_max_position_embeddings": 8192,
            "rope_type": "llama3",
        },
    ),
    "Qwen2-1.5B-Instruct": dict(
        block_size=32768,
        n_layer=28, # number of transformer layers
        n_head=12, # number of attention heads
        n_local_heads=2,
        dim=1536,
        intermediate_size=8960,
        vocab_size=151936,
        rope_base=1000000,
        attention_bias=True,
        norm_eps=1e-6,
        max_length=32768,
    ),
    "Qwen2-0.5B-Instruct": dict(
        block_size=32768,
        n_layer=24,
        n_head=14,
        n_local_heads=2,
        dim=896,
        intermediate_size=4864,
        vocab_size=151936,
        rope_base=1000000,
        attention_bias=True,
        norm_eps=1e-6,
        max_length=32768,
    ),
    "Qwen2-7B-Instruct": dict(
        block_size=32768,
        n_layer=28,
        n_head=28,
        n_local_heads=4,
        dim=3584,
        intermediate_size=18944,
        vocab_size=152064,
        rope_base=1000000,
        attention_bias=True,
        norm_eps=1e-6,
        max_length=32768,
    ),
}


class Transformer(nn.Module):
    def __init__(self, config: ModelArgs) -> None:
        super().__init__()
        self.config = config

        self.tok_embeddings = nn.Embedding(config.vocab_size, config.dim)
        self.layers = nn.ModuleList(
            TransformerBlock(config) for _ in range(config.n_layer)
        )
        self.norm = RMSNorm(config.dim, eps=config.norm_eps)
        self.output = nn.Linear(config.dim, config.vocab_size, bias=False)

        self.freqs_cis: Optional[Tensor] = None

        # Fixed for now
        self.max_batch_size = 1

    def setup_caches(self, **kwargs):
        cache_strategy = kwargs.pop("cache_strategy")

        head_dim = self.config.dim // self.config.n_head

        dtype = self.output.weight.dtype
        # For quantized layers, dtype is encoded in scales
        if hasattr(self.output, "scales"):
            dtype = self.output.scales.dtype
        elif hasattr(self.output, "scales_and_zeros"):
            dtype = self.output.scales_and_zeros.dtype
        for layer_idx, b in enumerate(self.layers):
            cache_constructor, relevant_kwargs = get_cache_constructor(
                cache_strategy=cache_strategy[layer_idx]
            )
            # Only pass in the kwargs we need for the cache we chose (useful especially for debugging)
            layerwise_keys = {
                "max_cache_length",
                "recent_window",
                "prompt_compression_strategy",
            }
            layer_kwargs = {
                k: kwargs[k][layer_idx] if k in layerwise_keys else kwargs[k]
                for k in relevant_kwargs
            }
            # layer_kwargs["layer_idx"] = layer_idx # @artij: to pass in layer_idx into the KVCacheHybrid
            b.attention.kv_cache = cache_constructor(
                self.max_batch_size,
                self.config.n_local_heads,
                head_dim,
                layer_idx,
                dtype,
                **layer_kwargs,
            )
            b.attention.prompt_compressor = get_prompt_compressor_constructor(
                kwargs["prompt_compression_strategy"][layer_idx]
            )(head_specific=b.attention.kv_cache.head_specific, **layer_kwargs)

        self.freqs_cis = precompute_freqs_cis(
            self.config.block_size,
            self.config.dim // self.config.n_head,
            self.config.rope_base,
            dtype,
            self.config.rope_scaling,
        )

    def reset_caches(self):
        for layer in self.layers:
            layer.attention.kv_cache.reset()

    def prompt_cache_overflow(self, prompt_length: int):
        return [
            prompt_length > layer.attention.kv_cache.max_cache_length
            for layer in self.layers
        ]

    def get_cache_stats(self, prompt_len, gen_len):
        stats = {}
        final_seq_len = prompt_len + gen_len
        avgs = defaultdict(list)
        mem_total = 0
        for layer_idx, layer in enumerate(self.layers):
            stat = layer.attention.kv_cache.compute_statistics(
                seq_len=torch.tensor(final_seq_len)
            )
            mem_total += stat.pop("cache_memory_gb")
            for k, v in stat.items():
                stats[f"{k}_{layer_idx}"] = v
                avgs[k].append(v)

        for k, v in avgs.items():
            stats[f"{k}_avg"] = sum(v) / len(v)

        stats["cache_memory_gb"] = mem_total
        return stats

    def min_cache_length(self):
        return min([layer.attention.kv_cache.max_cache_length for layer in self.layers])

    def forward(
        self,
        idx: Tensor,
        input_pos: Tensor,
        is_prefill: Tensor,
        mask: Optional[Tensor] = None,
        attn_top_k: Optional[float] = 1.0,
    ) -> Tensor:
        assert self.freqs_cis is not None, "Caches must be initialized first"
        freqs_cis = self.freqs_cis[input_pos]
        x = self.tok_embeddings(idx)

        for i, layer in enumerate(self.layers):
            x = layer(
                x,
                idx,
                input_pos,
                is_prefill,
                freqs_cis,
                mask,
                attn_top_k=attn_top_k,
            )
        x = self.norm(x)
        logits = self.output(x)
        return logits

    @classmethod
    def from_name(cls, name: str):
        return cls(ModelArgs.from_name(name))


class TransformerBlock(nn.Module):
    def __init__(self, config: ModelArgs) -> None:
        super().__init__()
        self.attention = Attention(config)
        self.feed_forward = FeedForward(config)
        self.ffn_norm = RMSNorm(config.dim, config.norm_eps)
        self.attention_norm = RMSNorm(config.dim, config.norm_eps)

    def forward(
        self,
        x: Tensor,
        input_ids: Tensor,
        input_pos: Tensor,
        is_prefill: Tensor,
        freqs_cis: Tensor,
        mask: Tensor,
        attn_top_k: Optional[float] = 1.0,
    ) -> Tensor:
        h = x + self.attention(
            self.attention_norm(x),
            input_ids,
            freqs_cis,
            mask,
            is_prefill,
            input_pos,
            attn_top_k=attn_top_k,
        )
        out = h + self.feed_forward(self.ffn_norm(h))
        return out


class Attention(nn.Module):
    def __init__(self, config: ModelArgs):
        super().__init__()
        assert config.dim % config.n_head == 0

        total_head_dim = (config.n_head + 2 * config.n_local_heads) * config.head_dim
        # key, query, value projections for all heads, but in a batch
        self.wqkv = nn.Linear(config.dim, total_head_dim, bias=config.attention_bias)
        self.wo = nn.Linear(config.dim, config.dim, bias=False)
        self.kv_cache = None
        self.prompt_compressor = None

        self.n_head = config.n_head
        self.head_dim = config.head_dim
        self.n_local_heads = config.n_local_heads
        self.dim = config.dim
        self._register_load_state_dict_pre_hook(self.load_hook)

    def load_hook(self, state_dict, prefix, *args):
        if prefix + "wq.weight" in state_dict:
            wq = state_dict.pop(prefix + "wq.weight")
            wk = state_dict.pop(prefix + "wk.weight")
            wv = state_dict.pop(prefix + "wv.weight")
            state_dict[prefix + "wqkv.weight"] = torch.cat([wq, wk, wv])

    def compress_prompt(self, input_pos, k_val, v_val, attn):
        seq_len = input_pos.shape[0]
        if self.kv_cache.max_cache_length < seq_len:
            kwargs = {"attn": attn}
            return self.prompt_compressor(input_pos, k_val, v_val, **kwargs)

        return input_pos, k_val, v_val, attn

    def forward(
        self,
        x: Tensor,
        input_ids: Tensor,
        freqs_cis: Tensor,
        mask: Tensor,
        is_prefill: bool,
        input_pos: Optional[Tensor] = None,
        attn_top_k: Optional[float] = 1.0,
    ) -> Tensor:
        bsz, seqlen, _ = x.shape

        kv_size = self.n_local_heads * self.head_dim
        q, k, v = self.wqkv(x).split([self.dim, kv_size, kv_size], dim=-1)

        q = q.view(bsz, seqlen, self.n_head, self.head_dim)
        k = k.view(bsz, seqlen, self.n_local_heads, self.head_dim)
        v = v.view(bsz, seqlen, self.n_local_heads, self.head_dim)

        q = apply_rotary_emb(q, freqs_cis)
        k = apply_rotary_emb(k, freqs_cis)

        q = q.transpose(1, 2)
        k = k.transpose(1, 2)
        v = v.transpose(1, 2)

        kv_mask = None
        cache_kwargs = {"input_ids": input_ids}
        if not is_prefill:
            k, v, kv_mask = self.kv_cache.update_kv(
                input_pos, k, v, is_prefill, **cache_kwargs
            )
            kv_mask = kv_mask.repeat_interleave(
                self.n_head // self.n_local_heads, dim=1
            )

        k_rep = k.repeat_interleave(self.n_head // self.n_local_heads, dim=1)
        v_rep = v.repeat_interleave(self.n_head // self.n_local_heads, dim=1)

        y, attn = scaled_dot_product_attention(
            q,
            k_rep,
            v_rep,
            attn_mask=kv_mask if mask is None else mask,
            dropout_p=0.0,
            attn_top_k=attn_top_k,
            # Ask the cache if needs attention scores returned (we cannot use FlexAttention if so)
            return_attn=self.kv_cache.return_attn(),
        )

        if (
            attn is not None
        ):  # Mean pool over the grouped queries (average over self.n_head // self.n_local_heads)
            attn = attn.view(
                bsz, self.n_local_heads, self.n_head // self.n_local_heads, seqlen, -1
            ).mean(dim=2)

        # Prefill updates happen after since we don't use the KV cache for prefill attention
        if is_prefill:
            input_pos, k, v, attn = self.compress_prompt(input_pos, k, v, attn)
            self.kv_cache.update_kv(input_pos, k, v, is_prefill, **cache_kwargs)

        # [Optional] Update the KV Cache internal state now that we have attention probabilities
        # This is a no-op for most cache classes
        self.kv_cache.update_state(input_pos, k, v, is_prefill, attn, **cache_kwargs)

        y = y.transpose(1, 2).contiguous().view(bsz, seqlen, self.dim)

        y = self.wo(y)
        return y


class FeedForward(nn.Module):
    def __init__(self, config: ModelArgs) -> None:
        super().__init__()
        self.w1 = nn.Linear(config.dim, config.intermediate_size, bias=False)
        self.w3 = nn.Linear(config.dim, config.intermediate_size, bias=False)
        self.w2 = nn.Linear(config.intermediate_size, config.dim, bias=False)

    def forward(self, x: Tensor) -> Tensor:
        return self.w2(F.silu(self.w1(x)) * self.w3(x))


class RMSNorm(nn.Module):
    def __init__(self, dim: int, eps: float = 1e-5):
        super().__init__()
        self.eps = eps
        self.weight = nn.Parameter(torch.ones(dim))

    def _norm(self, x):
        return x * torch.rsqrt(torch.mean(x * x, dim=-1, keepdim=True) + self.eps)

    def forward(self, x: Tensor) -> Tensor:
        output = self._norm(x.float()).type_as(x)
        return output * self.weight


def precompute_freqs_cis(
    seq_len: int,
    n_elem: int,
    base: int = 10000,
    dtype: torch.dtype = torch.bfloat16,
    rope_scaling: Optional[Dict[str, Any]] = None,
) -> Tensor:
    freqs = 1.0 / (
        base ** (torch.arange(0, n_elem, 2)[: (n_elem // 2)].float() / n_elem)
    )
    t = torch.arange(seq_len, device=freqs.device)
    if rope_scaling is not None:
        assert (
            rope_scaling["rope_type"] == "llama3"
        ), "Only Llama 3.1 scaling is supported"
        # Apply Llama 3.1 scaling
        low_freq_wavelen = (
            rope_scaling["original_max_position_embeddings"]
            / rope_scaling["low_freq_factor"]
        )
        high_freq_wavelen = (
            rope_scaling["original_max_position_embeddings"]
            / rope_scaling["high_freq_factor"]
        )
        new_freqs = []
        for freq in freqs:
            wavelen = 2 * math.pi / freq
            if wavelen < high_freq_wavelen:
                new_freqs.append(freq)
            elif wavelen > low_freq_wavelen:
                new_freqs.append(freq / rope_scaling["factor"])
            else:
                smooth = (
                    rope_scaling["original_max_position_embeddings"] / wavelen
                    - rope_scaling["low_freq_factor"]
                ) / (rope_scaling["high_freq_factor"] - rope_scaling["low_freq_factor"])
                new_freqs.append(
                    (1 - smooth) * freq / rope_scaling["factor"] + smooth * freq
                )
        freqs = torch.tensor(new_freqs, device=t.device)

    freqs = torch.outer(t, freqs)
    freqs_cis = torch.polar(torch.ones_like(freqs), freqs)
    cache = torch.stack([freqs_cis.real, freqs_cis.imag], dim=-1)
    return cache.to(dtype=dtype)


def apply_rotary_emb(x: Tensor, freqs_cis: Tensor) -> Tensor:
    xshaped = x.float().reshape(*x.shape[:-1], -1, 2)
    freqs_cis = freqs_cis.view(1, xshaped.size(1), 1, xshaped.size(3), 2)
    x_out2 = torch.stack(
        [
            xshaped[..., 0] * freqs_cis[..., 0] - xshaped[..., 1] * freqs_cis[..., 1],
            xshaped[..., 1] * freqs_cis[..., 0] + xshaped[..., 0] * freqs_cis[..., 1],
        ],
        -1,
    )

    x_out2 = x_out2.flatten(3)
    return x_out2.type_as(x)
