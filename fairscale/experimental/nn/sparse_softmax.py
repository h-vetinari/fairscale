# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.


from typing import Any, Tuple

import torch
from torch import nn
import torch.nn.functional as F

from .triton import fused_forward  # type: ignore

try:
    import faiss
    import faiss.contrib.torch_utils  # noqa: F401, actually used just by importing
except ImportError:
    faiss = None


try:
    from xformers.triton.softmax import softmax as triton_softmax
except ImportError:
    triton_softmax = None


def get_data(
    shape: Tuple[Tuple[int, int], Tuple[int, int]], dtype: torch.dtype = torch.float16, device: str = "cuda"
) -> Tuple[torch.Tensor, torch.nn.Parameter, torch.Tensor]:
    """ Utility function for getting some tensors for testing and benchmarking."""
    (tokens, d1), (d2, vocabs) = shape
    assert d1 == d2
    input = torch.rand(tokens, d1, device=device, dtype=dtype)
    weight = nn.Linear(d2, vocabs, bias=False, device=device, dtype=dtype).weight
    target = (torch.rand(tokens, device=device) * vocabs).long()
    return input, weight, target


class BaselineSoftmax(nn.Module):
    """ Baseline softmax that does an output projection and a softmax. """

    def __init__(
        self, proj_weight: torch.nn.Parameter, k: int = 0, tile_factor: int = 0, log: bool = True
    ):  # k, tile_factor are ignored.
        super().__init__()
        out_dim, in_dim = proj_weight.shape
        self.fc = nn.Linear(in_dim, out_dim, bias=False, device="cuda", dtype=proj_weight.dtype)
        self.fc.weight = proj_weight
        assert self.fc.weight.dtype in [torch.float16, torch.float32], self.fc.weight.dtype
        self.fp16 = self.fc.weight.dtype == torch.float16
        self.log = log

    def forward(self, *input: Any, **kwargs: Any) -> Any:
        assert kwargs == {}
        input, target = input
        assert isinstance(input, torch.Tensor)
        assert isinstance(target, torch.Tensor)
        if self.fp16:
            assert input.dtype == torch.float16
        x = self.fc(input)
        if self.log:
            x = F.log_softmax(x, dim=-1, dtype=torch.float32)
        else:
            x = F.softmax(x, dim=-1, dtype=torch.float32)
        assert x.dtype == torch.float32
        return x


class BaselineSoftmaxNllLoss(BaselineSoftmax):
    """ Baseline that does an output projection, a softmax NLL loss. """

    def __init__(
        self, proj_weight: torch.nn.Parameter, k: int = 0, tile_factor: int = 0, log: bool = True
    ):  # k, tile_factor are ignored.
        super().__init__(proj_weight, k, tile_factor, log)

    def forward(self, *input: Any, **kwargs: Any) -> Any:
        assert kwargs == {}
        input, target = input
        assert isinstance(input, torch.Tensor)
        assert isinstance(target, torch.Tensor)
        if self.fp16:
            assert input.dtype == torch.float16
        x = self.fc(input)
        if self.log:
            x = F.log_softmax(x, dim=-1, dtype=torch.float32)
        else:
            x = F.softmax(x, dim=-1, dtype=torch.float32)
        assert x.dtype == torch.float32
        x = F.nll_loss(x, target)
        x = x.mean()
        return x


class TritonSoftmax(BaselineSoftmax):
    """ Softmax that uses xformers' softmax. """

    def __init__(self, proj_weight: torch.nn.Parameter, k: int = 0, tile_factor: int = 0):  # k, tile_factor is ignored.
        super().__init__(proj_weight)
        assert triton_softmax is not None, "Need to import xformers"
        assert proj_weight.dtype == torch.float32, "fp16 not yet supported"

    def forward(self, *input: Any, **kwargs: Any) -> Any:
        assert kwargs == {}
        input, target = input
        assert isinstance(input, torch.Tensor)
        assert isinstance(target, torch.Tensor)
        x = self.fc(input)
        orig_shape = x.shape
        div = min(orig_shape[0], 128)
        assert x.shape[0] % div == 0
        x = triton_softmax(x.reshape(-1, div, x.shape[1]))
        return x.reshape(orig_shape)


class TritonFuseAll(nn.Module):
    """ Triton fuse fc + softmax + nll_loss. """

    def __init__(self, proj_weight: torch.nn.Parameter, k: int = 0, tile_factor: int = 0):  # k, tile_factor is ignored.
        super().__init__()
        self.weight = proj_weight

    def forward(self, *input: Any, **kwargs: Any) -> Any:
        assert kwargs == {}
        input, target = input
        assert isinstance(input, torch.Tensor)
        assert isinstance(target, torch.Tensor)
        return fused_forward(input, self.weight.data, target)


class InplaceSoftmax(nn.Module):
    """ Inplace softmax that saves half of the memory but
        this does NOT work with autograd, which means it can't
        be used in training.
    """

    def __init__(self, proj_weight: torch.nn.Parameter, k: int = 0):  # k is ignored.
        super().__init__()
        assert proj_weight.dtype == torch.float32, "fp16 not yet supported"
        out_dim, in_dim = proj_weight.shape
        self.fc = nn.Linear(in_dim, out_dim, bias=False, device="cuda")
        self.fc.weight = proj_weight

    def forward(self, *input: Any, **kwargs: Any) -> Any:
        assert kwargs == {}
        input, target = input
        assert isinstance(input, torch.Tensor)
        assert isinstance(target, torch.Tensor)
        x = self.fc(input)
        x.exp_()
        x_sum = torch.sum(x, dim=-1, keepdim=True)
        x /= x_sum
        return x


class TiledSoftmax(nn.Module):
    """ Memory saving softmax that does the softmax in a micro-batched fashion.

        Peak memory is saved only when torch.no_grad is used. Which means this
        needs activation checkpointing during training.

        A custom backward is used to recompute the grad also in this micro-batched fashion.
    """

    def __init__(
        self, proj_weight: torch.nn.Parameter, k: int = 0, tile_factor: int = 16, log: bool = True
    ):  # k is ignored
        super().__init__()
        out_dim, in_dim = proj_weight.shape
        self.fc = nn.Linear(in_dim, out_dim, bias=False, device="cuda", dtype=proj_weight.dtype)
        self.fc.weight = proj_weight
        assert self.fc.weight.dtype in [torch.float16, torch.float32]
        self.fp16 = self.fc.weight.dtype == torch.float16
        self.tile_factor = tile_factor
        self.log = log

    def forward(self, *input: Any, **kwargs: Any) -> Any:
        assert kwargs == {}
        input, target = input
        assert isinstance(input, torch.Tensor)
        assert isinstance(target, torch.Tensor)
        if self.fp16:
            assert input.dtype == torch.float16
        tokens, _ = input.shape
        with torch.no_grad():
            out = torch.empty(tokens, self.fc.weight.shape[0], dtype=input.dtype, device=input.device)
            for x, y in zip(
                torch.split(input, tokens // self.tile_factor, 0), torch.split(out, tokens // self.tile_factor, 0)
            ):
                x = self.fc(x)
                if self.log:
                    x = F.log_softmax(x, dim=-1, dtype=torch.float32)
                else:
                    x = F.softmax(x, dim=-1, dtype=torch.float32)
                assert x.dtype == torch.float32
                y.copy_(x)
        # Do not use torch.cat(out, dim=0), which would double the memory.
        return out


class TopKSoftmax(nn.Module):
    """ TopK softmax that does a projection and take the top-K and then softmax.

        Peak GPU memory is not reduced and actually increased since
        we need to add in the target and generate a mask and return a full
        output after softmax.
    """

    def __init__(self, proj_weight: torch.nn.Parameter, k: int):
        super().__init__()
        assert proj_weight.dtype == torch.float32, "fp16 not yet supported"
        out_dim, in_dim = proj_weight.shape
        self.fc = nn.Linear(in_dim, out_dim, bias=False, device="cuda")
        self.fc.weight = proj_weight
        self.k = k

    def forward(self, *input: Any, **kwargs: Any) -> Any:
        assert kwargs == {}
        input, target = input
        assert isinstance(input, torch.Tensor)
        assert isinstance(target, torch.Tensor)
        # Get matmul output.
        x = self.fc(input)
        # Get the top-K index.
        D, I = torch.topk(x, self.k)
        # Add in the targets. (Till here only use 1GB for 1k case)
        I = torch.cat([I, target.reshape(-1, 1)], dim=1)
        # Generate a mask. (The following line triples memory usage.)
        mask = torch.ones_like(x) * float("-inf")
        mask.scatter_(1, I, 0.0)
        # Mask x and softmax it.
        x = x + mask
        x = F.softmax(x, dim=-1)
        return x


class TopKTiledSoftmax(nn.Module):
    """ TopK softmax that does a projection and take the top-K and then softmax.

        Peak GPU memory is reduced since the computation is done in tiled fashion.
        The tiling is done on the `out_dim` of the FC weight tensor.
    """

    def __init__(self, proj_weight: torch.nn.Parameter, k: int, tile_factor: int = 16, log: bool = True):
        super().__init__()
        out_dim, in_dim = proj_weight.shape
        self.vocabs = out_dim
        self.fcs = []
        for w in torch.split(proj_weight, out_dim // tile_factor, 0):
            self.fcs.append(nn.Linear(in_dim, w.shape[0], bias=False, device="cuda", dtype=proj_weight.dtype))
            delattr(self.fcs[-1], "weight")
            self.fcs[-1].weight = w  # type: ignore
        self.k = k
        self.weight = proj_weight.T
        assert self.weight.dtype in [torch.float16, torch.float32]
        self.fp16 = self.weight.dtype == torch.float16
        self.log = log

    def forward(self, *input: Any, **kwargs: Any) -> Any:
        assert kwargs == {}
        input, target = input
        assert isinstance(input, torch.Tensor)
        assert isinstance(target, torch.Tensor)
        if self.fp16:
            assert input.dtype == torch.float16
        tokens, _ = input.shape
        val = []
        idx = []
        base = 0
        for fc in self.fcs:
            # Get matmul output.
            x = fc(input)
            # Get the top-K value and index.
            D, I = torch.topk(
                x, min(self.k, x.shape[-1])
            )  # some tiles (esp. last tile) may have small output dim < self.k
            val.append(D)
            idx.append(I + base)
            base += fc.weight.shape[0]
        # Top-K again, after this only uses 1/10th of memory of untiled case.
        val = torch.cat(val, dim=-1)
        idx = torch.cat(idx, dim=-1)
        val, I = torch.topk(val, self.k)
        idx = torch.gather(idx, 1, I)

        # Make a sparse tensor from the top-k results.
        j = torch.arange(idx.shape[0], device=idx.device).expand(idx.shape[1], idx.shape[0]).T
        idx = torch.cat([j.reshape(1, -1), idx.reshape(1, -1)])
        result = torch.sparse_coo_tensor(idx, val.reshape(-1), (tokens, self.vocabs))

        # Make another sparse tensor from the targets.
        w = torch.gather(self.weight, 1, target.expand(self.weight.shape[0], target.shape[0]))
        val = (input * w.T).sum(dim=1)
        j = torch.arange(target.shape[0], device=target.device).expand(1, target.shape[0]).T
        idx = torch.cat([j.reshape(1, -1), target.reshape(1, -1)])
        result_target = torch.sparse_coo_tensor(idx, val.reshape(-1), (tokens, self.vocabs))

        # Add in the targets to top-K results.
        result += result_target

        # Softmax (assuming fill value is -inf) and return the dense result
        if self.fp16:
            result = result.float()  # passing dtype=torch.float32 only works on CPU, not cuda.
        if self.log:
            x = torch.sparse.log_softmax(result, dim=1).to_dense()
        else:
            x = torch.sparse.softmax(result, dim=1).to_dense()
        assert x.dtype == torch.float32
        return x


_res = None


class TopKFaissSoftmax(nn.Module):
    """ TopK softmax that uses FAISS's fused project & top-K to reduce GPU memory.

        Note, the output of this kernel is reduced in size. It is no longer the
        [tokens, vocabs] shape. Instead, it is in the shape of [k+1, vocabs].
    """

    def __init__(self, proj_weight: torch.nn.Parameter, k: int):
        super().__init__()
        assert faiss is not None, "Need to pip install FAISS"
        assert proj_weight.dtype == torch.float32, "fp16 not yet supported"
        self.k = k
        global _res
        if _res is None:
            _res = faiss.StandardGpuResources()
            _res.setTempMemory(1024 * 1024 * 10)
        self.b = proj_weight.data

    def forward(self, *input: Any, **kwargs: Any) -> Any:
        assert kwargs == {}
        input, target = input
        # Need to sync the GPU to avoid errors from previous async kernels affecting this call.
        torch.cuda.synchronize()
        # Do the fast top-k.
        D, I = faiss.knn_gpu(_res, input, self.b, self.k)
        # XXX: no way to add in target; need custom backward
        return D
