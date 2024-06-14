"""Adapted from the Triton documentation (https://triton-lang.org/main/index.html)."""
import os

import torch

import triton
import triton.language as tl

# Allow debugging of the Triton kernel.
# os.environ["TRITON_INTERPRET"] = "1"

@triton.jit
def add_kernel(
    x_ptr,
    y_ptr,
    output_ptr,
    n_elements,
    BLOCK_SIZE: tl.constexpr,  # Number of elements each 'program' should process.
):
    # There are multiple 'programs' each processing different data.
    # Because we will use a 1D launch grid, we set the "axis" to 0.
    pid = tl.program_id(axis=0)

    # This program will process inputs that are offset from the initial data.
    # For instance, if you had a vector of length 256 and block_size of 64, the programs
    # would each access the elements [0:64, 64:128, 128:192, 192:256].
    # Note that offsets is a list of pointers:
    block_start = pid * BLOCK_SIZE
    offsets = block_start + tl.arange(0, BLOCK_SIZE)

    # Use a mask to avoid out-of-bounds memory accesses.
    mask = offsets < n_elements

    # Load x and y from DRAM, masking out any extra elements in case the 
    # size of the vector (`n_elements`) is not a multiple of the block size
    # (this would only affect the last 'program' as this handles the 'last' block). 
    x = tl.load(x_ptr + offsets, mask=mask)
    y = tl.load(y_ptr + offsets, mask=mask)

    # Perform the addition and write the result back to DRAM
    output = x + y
    tl.store(output_ptr + offsets, output, mask=mask)


def add(x: torch.Tensor, y: torch.Tensor):
    # Need to pre-allocate device memory for the output
    output = torch.empty_like(x)

    # Verify that the input and output tensors are on the GPU
    assert x.is_cuda and y.is_cuda and output.is_cuda

    # Extract the size of the output
    n_elements = output.numel()

    # The Single Program Multiple Data (SPDM) launch grid indicates the number of 
    # "kernel instances" that run in parallel. It is analogous to CUDA launch grids.
    # In this case, we use a 1D grid where the size is the number of blocks.
    grid = lambda metaparams: (triton.cdiv(n_elements, metaparams['BLOCK_SIZE']), )
    
    # Launch the Triton kernel
    # Note that
    #  - Each `torch.tensor` object is implicitly converted into a pointer to its first element.
    #  - The `triton.jit`-decorated function is indexed with a launch grid to obtain a callable GPU kernel.
    #  - Don't forget to pass metaparameters (needed in `grid`) as kwargs, e.g. `BLOCK_SIZE` here.
    add_kernel[grid](x, y, output, n_elements, BLOCK_SIZE=1024)
    
    # We return a handle to z but, since `torch.cuda.synchronize()` hasn't been called, the kernel is still
    # running asynchronously at this point.
    return output

@triton.testing.perf_report(
    triton.testing.Benchmark(
        x_names=['size'],  # Argument names to use as an x-axis for the plot.
        x_vals=[2**i for i in range(12, 28, 1)],  # Possible values for `x_name` (here 2^12 to 2^27)
        x_log=True,
        line_arg='provider',  # Each value of `line_arg` identifies a line in the plot.
        line_vals=['triton', 'torch'],  # Possible values of `line_arg` (here `triton` and `torch`)
        line_names=['Triton', 'Torch'],
        styles=[('blue', '-'), ('red', '-')],
        ylabel='Global Memory Bandwidth (GB/s)',
        plot_name='vector_add_benchmarks',
        args={},
    )
)

def benchmark(size, provider):
    # Initialise inputs randomly
    x = torch.rand(size, device='cuda', dtype=torch.float32)
    y = torch.rand(size, device='cuda', dtype=torch.float32)

    # Benchmark the runtime, getting the median, 20th pecentile, and 80th percentile.
    quantiles = [0.5, 0.2, 0.8]
    if provider == 'torch':
        ms, min_ms, max_ms = triton.testing.do_bench(lambda: x + y, quantiles=quantiles)
    if provider == 'triton':
        ms, min_ms, max_ms = triton.testing.do_bench(lambda: add(x, y), quantiles=quantiles)
    
    # Compute the global memory bandwidth in GB/s. This indicates the data transfer rate. 
    # The factor of 3 is because we have three tensors (`x`, `y`, `output`). 
    # `x.numel()` gives the total number of elements in `x`. 
    # `x.element_size()` gives the size in bytes of an individual element of `x`
    # Thus, the formula basically says:
    # Global memory bandwidth in GB/s = (#Bytes)/(Runtime in ms)*(1e-9 GB/B)*(1e3 ms/s)
    gbps = lambda ms: 3 * x.numel() * x.element_size() / ms * 1e-6

    return gbps(ms), gbps(max_ms), gbps(min_ms)

if __name__ == "__main__":
    torch.manual_seed(0)
    size = 98432
    x = torch.rand(size, device='cuda')
    y = torch.rand(size, device='cuda')
    output_torch = x + y
    output_triton = add(x, y)
    print(output_torch)
    print(output_triton)
    print(f'The maximum difference between torch and triton is '
        f'{torch.max(torch.abs(output_torch - output_triton))}')

    # Compare performance of custom Triton kernel and PyTorch. Durations are in milliseconds.
    benchmark.run(print_data=True, save_path="/home/danielluo/cuda-c/benchmarks/")
