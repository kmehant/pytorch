# Owner(s): ["module: c10d"]
import unittest
from typing import List

import torch
import torch.distributed as dist
import torch.distributed._functional_collectives as funcol
from torch._C import FileCheck
from torch._dynamo.utils import same
from torch._inductor.utils import fresh_inductor_cache, run_and_get_triton_code
from torch.distributed._functional_collectives import (
    all_gather_into_tensor_coalesced,
    all_gather_tensor,
    all_reduce,
    all_reduce_coalesced,
    all_to_all_single,
    AsyncCollectiveTensor,
    reduce_scatter_tensor,
    reduce_scatter_tensor_coalesced,
)
from torch.testing._internal.common_distributed import (
    MultiProcessTestCase,
    requires_nccl,
    skip_if_lt_x_gpu,
)
from torch.testing._internal.common_utils import run_tests
from torch.utils._triton import has_triton


def load_test_module(name):
    import sys
    from importlib.machinery import SourceFileLoader
    from pathlib import Path
    from unittest import mock

    testdir = Path(__file__).absolute().parent.parent
    with mock.patch("sys.path", [*sys.path, str(testdir)]):
        return SourceFileLoader(
            name, str(testdir / f"{name.replace('.', '/')}.py")
        ).load_module()


AOTIRunnerUtil = load_test_module("inductor.test_aot_inductor_utils").AOTIRunnerUtil

import sys

if not dist.is_available():
    print("distributed package not available, skipping tests", file=sys.stderr)
    sys.exit(0)


@requires_nccl()
class C10DFunctionalNativeTest(MultiProcessTestCase):
    def setUp(self) -> None:
        super().setUp()
        funcol.enable_native_funcol()
        self._spawn_processes()

    @property
    def world_size(self) -> int:
        return 2

    @property
    def ranks(self) -> List[int]:
        return list(range(self.world_size))

    @property
    def device(self) -> torch.device:
        return torch.device(f"cuda:{self.rank}")

    def _init_process_group(self) -> None:
        # Allow testing aoti after torch.compile
        torch._inductor.config.triton.store_cubin = True
        torch._inductor.config.debug = True

        torch.cuda.set_device(self.device)
        store = dist.FileStore(self.file_name, self.world_size)
        dist.init_process_group(
            backend="nccl",
            world_size=self.world_size,
            rank=self.rank,
            store=store,
        )
        torch._C._distributed_c10d._register_process_group("default", dist.group.WORLD)

    @skip_if_lt_x_gpu(2)
    def test_all_reduce(self) -> None:
        self._init_process_group()

        input = torch.full((10, 10), float(self.rank), device=self.device)
        output = torch.ops._c10d_functional.all_reduce(
            input,
            "avg",
            "default",
        )
        output = torch.ops._c10d_functional.wait_tensor(output)
        assert id(output) != id(input)
        expect = sum(self.ranks) / self.world_size
        assert output.eq(expect).all()

        # Test Python API and AsyncCollectiveTensor
        output = all_reduce(
            input,
            "avg",
            "default",
        )
        assert isinstance(output, AsyncCollectiveTensor)
        assert not output.completed
        assert output.eq(expect).all()
        assert output.completed

    @skip_if_lt_x_gpu(2)
    def test_all_reduce_(self) -> None:
        self._init_process_group()

        input = torch.full((10, 10), float(self.rank), device=self.device)
        output = torch.ops._c10d_functional.all_reduce_(
            input,
            "avg",
            "default",
        )
        output = torch.ops._c10d_functional.wait_tensor(output)
        assert id(output) == id(input)
        expect = sum(self.ranks) / self.world_size
        assert output.eq(expect).all()

    @skip_if_lt_x_gpu(2)
    def test_all_reduce_coalesced(self) -> None:
        self._init_process_group()

        inputs = [
            torch.full((i, i), float(self.rank * i), device=self.device)
            for i in range(10)
        ]
        outputs = torch.ops._c10d_functional.all_reduce_coalesced(
            inputs,
            "avg",
            "default",
        )
        for i, (output, input) in enumerate(zip(outputs, inputs)):
            output = torch.ops._c10d_functional.wait_tensor(output)
            assert id(output) != id(input)
            assert output.eq(sum(self.ranks) / self.world_size * i).all()

        # Test Python API and AsyncCollectiveTensor
        outputs = all_reduce_coalesced(
            inputs,
            "avg",
            "default",
        )
        for i, (output, input) in enumerate(zip(outputs, inputs)):
            assert not output.completed
            assert output.eq(sum(self.ranks) / self.world_size * i).all()
            assert output.completed

    @skip_if_lt_x_gpu(2)
    def test_all_reduce_coalesced_(self) -> None:
        self._init_process_group()

        inputs = [
            torch.full((i, i), float(self.rank * i), device=self.device)
            for i in range(10)
        ]
        outputs = torch.ops._c10d_functional.all_reduce_coalesced_(
            inputs,
            "avg",
            "default",
        )
        for i, (output, input) in enumerate(zip(outputs, inputs)):
            output = torch.ops._c10d_functional.wait_tensor(output)
            assert id(output) == id(input)
            assert output.eq(sum(self.ranks) / self.world_size * i).all()

    @skip_if_lt_x_gpu(2)
    def test_all_gather_into_tensor(self) -> None:
        self._init_process_group()

        input = torch.full((10, 10), float(self.rank), device=self.device)
        output = torch.ops._c10d_functional.all_gather_into_tensor(
            input,
            self.world_size,
            "default",
        )
        output = torch.ops._c10d_functional.wait_tensor(output)
        expect = torch.cat(
            [
                torch.full((10, 10), float(rank), device=self.device)
                for rank in self.ranks
            ]
        )
        assert torch.allclose(output, expect)
        assert output.eq(expect).all()

        # Test Python API and AsyncCollectiveTensor
        output = all_gather_tensor(
            input,
            0,
            "default",
        )
        assert isinstance(output, AsyncCollectiveTensor)
        assert not output.completed
        assert output.eq(expect).all()
        assert output.completed

    @skip_if_lt_x_gpu(2)
    def test_all_gather_into_tensor_coalesced(self) -> None:
        self._init_process_group()

        inputs = [
            torch.full((10, 10), float(self.rank * i), device=self.device)
            for i in range(10)
        ]
        outputs = torch.ops._c10d_functional.all_gather_into_tensor_coalesced(
            inputs,
            self.world_size,
            "default",
        )
        expect = [
            torch.cat(
                [
                    torch.full((10, 10), float(rank) * i, device=self.device)
                    for rank in self.ranks
                ]
            )
            for i in range(10)
        ]
        for i, output in enumerate(outputs):
            output = torch.ops._c10d_functional.wait_tensor(output)
            assert output.eq(expect[i]).all()

        # Test Python API and AsyncCollectiveTensor
        outputs = all_gather_into_tensor_coalesced(
            inputs,
            "default",
        )
        for i, output in enumerate(outputs):
            assert not output.completed
            assert output.eq(expect[i]).all()
            assert output.completed

    @skip_if_lt_x_gpu(2)
    def test_reduce_scatter_tensor(self) -> None:
        self._init_process_group()

        input = torch.tensor(self.ranks, device=self.device)
        output = torch.ops._c10d_functional.reduce_scatter_tensor(
            input,
            "avg",
            self.world_size,
            "default",
        )
        output = torch.ops._c10d_functional.wait_tensor(output)
        assert output.eq(self.rank).all()

        # Test Python API and AsyncCollectiveTensor
        output = reduce_scatter_tensor(
            input,
            "avg",
            0,
            "default",
        )
        assert isinstance(output, AsyncCollectiveTensor)
        assert not output.completed
        assert output.eq(self.rank).all()
        assert output.completed

    @skip_if_lt_x_gpu(2)
    def test_reduce_scatter_tensor_coalesced(self) -> None:
        self._init_process_group()

        inputs = [torch.tensor(self.ranks, device=self.device) * i for i in range(10)]
        outputs = torch.ops._c10d_functional.reduce_scatter_tensor_coalesced(
            inputs,
            "avg",
            self.world_size,
            "default",
        )
        for i, output in enumerate(outputs):
            output = torch.ops._c10d_functional.wait_tensor(output)
            assert output.eq(self.rank * i).all()

        # Test Python API and AsyncCollectiveTensor
        outputs = reduce_scatter_tensor_coalesced(
            inputs,
            "avg",
            [0] * 10,
            "default",
        )
        for i, output in enumerate(outputs):
            assert not output.completed
            assert output.eq(self.rank * i).all()
            assert output.completed

    @skip_if_lt_x_gpu(2)
    def test_all_to_all_single(self) -> None:
        self._init_process_group()
        torch.cuda.set_device(self.device)

        torch.manual_seed(42)
        send_sz_matrix = torch.randint(0, 20, (self.world_size, self.world_size))

        input_split_sizes = send_sz_matrix[self.rank].tolist()
        output_split_sizes = send_sz_matrix[:, self.rank].tolist()
        input = torch.full((sum(input_split_sizes),), float(self.rank)).cuda()

        output = torch.ops._c10d_functional.all_to_all_single(
            input,
            output_split_sizes,
            input_split_sizes,
            "default",
        )
        output = torch.ops._c10d_functional.wait_tensor(output)
        expect = torch.cat(
            [
                torch.full((sz,), float(rank)).cuda()
                for rank, sz in enumerate(output_split_sizes)
            ]
        )
        assert output.eq(expect).all()

        # Test Python API and AsyncCollectiveTensor
        output = all_to_all_single(
            input, output_split_sizes, input_split_sizes, "default"
        )
        assert not output.completed
        assert output.eq(expect).all()
        assert output.completed

    @unittest.skipIf(not has_triton(), "Inductor+gpu needs triton and recent GPU arch")
    @torch._inductor.config.patch(debug=True)
    @fresh_inductor_cache()
    def test_inductor_all_reduce_single(self):
        self._init_process_group()

        def func(arg: torch.Tensor) -> torch.Tensor:
            buf0 = arg + 42
            # Expect in-place with inductor allocated buf
            ar0 = torch.ops._c10d_functional.all_reduce(buf0, "avg", "default")
            ar0 = torch.ops._c10d_functional.wait_tensor(ar0)
            # Expect no in-place with graph input
            ar1 = torch.ops._c10d_functional.all_reduce(arg, "avg", "default")
            ar1 = torch.ops._c10d_functional.wait_tensor(ar1)
            return ar0, ar1

        arg = torch.rand(4, 4, device=self.device)
        compiled = torch.compile(func)

        code = run_and_get_triton_code(compiled, arg)
        (
            FileCheck()
            .check("buf0 = empty(")
            .check("buf5 = empty(")
            # Expect in-place with inductor allocated buf
            .check("torch.ops._c10d_functional.all_reduce_.default(buf0")
            .check("torch.ops._c10d_functional.wait_tensor.default(buf0")
            # Expect no in-place with graph input (buf5 is a clone)
            .check("torch.ops._c10d_functional.all_reduce_.default(buf5")
            .check("torch.ops._c10d_functional.wait_tensor.default(buf5")
            # Expect no extra copy on return
            .check("return (buf0, buf5, )")
            .run(code)
        )
        out = compiled(arg)
        correct = func(arg)
        assert same(out, correct), f"{out} va {correct}"

        # Test aoti
        out = AOTIRunnerUtil.run("cuda", func, (arg,))
        torch.cuda.synchronize()

    @unittest.skipIf(not has_triton(), "Inductor+gpu needs triton and recent GPU arch")
    @torch._inductor.config.patch(debug=True)
    @fresh_inductor_cache()
    def test_inductor_all_reduce_coalesced(self):
        self._init_process_group()

        def func(args: List[torch.Tensor]) -> torch.Tensor:
            bufs = [arg + 42 for arg in args]
            # Expect in-place with inductor allocated buf
            ar0 = torch.ops._c10d_functional.all_reduce_coalesced(
                bufs, "avg", "default"
            )
            ar0 = [torch.ops._c10d_functional.wait_tensor(out) for out in ar0]
            # Expect no in-place with graph input
            ar1 = torch.ops._c10d_functional.all_reduce_coalesced(
                args, "avg", "default"
            )
            ar1 = [torch.ops._c10d_functional.wait_tensor(out) for out in ar1]
            return ar0, ar1

        args = [torch.rand(4, 4, device=self.device) for _ in range(2)]
        compiled = torch.compile(func)
        code = run_and_get_triton_code(compiled, args)
        (
            FileCheck()
            .check("buf0 = empty(")
            .check("buf5 = empty(")
            .check("buf1 = empty(")
            .check("buf6 = empty(")
            # Expect in-place with inductor allocated buf
            .check(
                "torch.ops._c10d_functional.all_reduce_coalesced_"
                ".default([buf0, buf1]"
            )
            # Expect no in-place with graph input (buf5, buf6 are clones)
            .check(
                "torch.ops._c10d_functional.all_reduce_coalesced_"
                ".default([buf5, buf6]"
            )
            .check("torch.ops._c10d_functional.wait_tensor.default(buf0")
            .check("torch.ops._c10d_functional.wait_tensor.default(buf1")
            .check("torch.ops._c10d_functional.wait_tensor.default(buf5")
            .check("torch.ops._c10d_functional.wait_tensor.default(buf6")
            # Expect no extra copy on return
            .check("return (buf0, buf1, buf5, buf6, )")
            .run(code)
        )
        out = compiled(args)
        correct = func(args)
        assert same(out, correct), f"{out} va {correct}"

        # Test aoti
        out = AOTIRunnerUtil.run("cuda", func, (args,))
        torch.cuda.synchronize()

    @unittest.skipIf(not has_triton(), "Inductor+gpu needs triton and recent GPU arch")
    @torch._inductor.config.patch(debug=True)
    @fresh_inductor_cache()
    def test_inductor_inplace_op_on_view(self):
        self._init_process_group()

        def func(arg: torch.Tensor) -> torch.Tensor:
            buf0 = (arg + 10)[:2]
            ar0 = torch.ops._c10d_functional.all_reduce(buf0, "avg", "default")
            ar0 = torch.ops._c10d_functional.wait_tensor(ar0)
            return ar0

        arg = torch.rand(4, 4, device=self.device)
        compiled = torch.compile(func)

        code = run_and_get_triton_code(compiled, arg)
        (
            FileCheck()
            .check("buf0 = empty(")
            # Ensure the all_reduce_ input is a view
            .check(
                "torch.ops._c10d_functional.all_reduce_.default(reinterpret_tensor(buf0"
            )
            .check(
                "torch.ops._c10d_functional.wait_tensor.default(reinterpret_tensor(buf0"
            )
            .check("return (reinterpret_tensor(buf0")
            .run(code)
        )
        out = compiled(arg)
        correct = func(arg)
        assert same(out, correct), f"{out} va {correct}"

    @unittest.skipIf(not has_triton(), "Inductor+gpu needs triton and recent GPU arch")
    @torch._inductor.config.patch(debug=True)
    @fresh_inductor_cache()
    def test_inductor_reuse_buffer_after_inplace_collective(self):
        self._init_process_group()

        def func(arg: torch.Tensor) -> torch.Tensor:
            # Expect allocation
            buf0 = arg + 42
            ar0 = torch.ops._c10d_functional.all_reduce(buf0, "avg", "default")
            ar0 = torch.ops._c10d_functional.wait_tensor(ar0)
            # Expect allocation
            buf1 = torch.mm(arg, ar0)
            # Expect buf0 to be reused
            buf2 = torch.mm(arg, buf1)
            return buf1, buf2

        arg = torch.rand(4, 4, device=self.device)
        compiled = torch.compile(func)
        code = run_and_get_triton_code(compiled, arg)
        (
            FileCheck()
            # Expect allocation
            .check("buf0 = empty(")
            .check("torch.ops._c10d_functional.all_reduce_.default(buf0")
            .check("torch.ops._c10d_functional.wait_tensor.default(buf0")
            # Expect allocation
            .check("buf5 = empty(")
            .check("extern_kernels.mm(arg0_1, buf0, out=buf5")
            # Expect buf0 to be reused
            .check("buf6 = buf0; del buf0  # reuse")
            .check("extern_kernels.mm(arg0_1, buf5, out=buf6")
            # Expect no extra copy on return
            .check("return (buf5, buf6, )")
            .run(code)
        )
        out = compiled(arg)
        correct = func(arg)
        assert same(out, correct), f"{out} va {correct}"

    @unittest.skipIf(not has_triton(), "Inductor+gpu needs triton and recent GPU arch")
    @torch._inductor.config.patch(debug=True)
    @fresh_inductor_cache()
    def test_inductor_all_gather_into_tensor_single(self):
        self._init_process_group()

        def func(arg: torch.Tensor) -> torch.Tensor:
            ag0 = torch.ops._c10d_functional.all_gather_into_tensor(
                arg, self.world_size, "default"
            )
            ag0 = torch.ops._c10d_functional.wait_tensor(ag0)
            return ag0

        arg = torch.rand(4, 4, device=self.device)
        compiled = torch.compile(func)
        code = run_and_get_triton_code(compiled, arg)
        (
            FileCheck()
            .check(
                "buf0 = torch.ops._c10d_functional.all_gather_into_tensor.default(arg0_1"
            )
            .check("torch.ops._c10d_functional.wait_tensor.default(buf0")
            # Expect no extra copy on return
            .check("return (buf0, )")
            .run(code)
        )
        out = compiled(arg)
        correct = func(arg)
        assert same(out, correct), f"{out} va {correct}"

        # Test aoti
        out = AOTIRunnerUtil.run("cuda", func, (arg,))
        torch.cuda.synchronize()

    @unittest.skipIf(not has_triton(), "Inductor+gpu needs triton and recent GPU arch")
    @torch._inductor.config.patch(debug=True)
    @fresh_inductor_cache()
    def test_inductor_all_gather_into_tensor_coalesced(self):
        self._init_process_group()

        def func(args: List[torch.Tensor]) -> torch.Tensor:
            ag0 = torch.ops._c10d_functional.all_gather_into_tensor_coalesced(
                args, self.world_size, "default"
            )
            ag0 = [torch.ops._c10d_functional.wait_tensor(out) for out in ag0]
            return ag0

        args = [torch.rand(4, 4, device=self.device) for _ in range(4)]
        compiled = torch.compile(func)
        code = run_and_get_triton_code(compiled, args)
        (
            FileCheck()
            .check(
                "buf0 = torch.ops._c10d_functional.all_gather_into_tensor_coalesced"
                ".default([arg0_1, arg1_1, arg2_1, arg3_1]"
            )
            .check("buf1 = buf0[0]")
            .check("buf2 = buf0[1]")
            .check("buf3 = buf0[2]")
            .check("buf4 = buf0[3]")
            .check("torch.ops._c10d_functional.wait_tensor.default(buf1")
            .check("torch.ops._c10d_functional.wait_tensor.default(buf2")
            .check("torch.ops._c10d_functional.wait_tensor.default(buf3")
            .check("torch.ops._c10d_functional.wait_tensor.default(buf4")
            # Expect no extra copy on return
            .check("return (buf1, buf2, buf3, buf4, )")
            .run(code)
        )
        out = compiled(args)
        correct = func(args)
        assert same(out, correct), f"{out} va {correct}"

        # Test aoti
        out = AOTIRunnerUtil.run("cuda", func, (args,))
        torch.cuda.synchronize()

    @unittest.skipIf(not has_triton(), "Inductor+gpu needs triton and recent GPU arch")
    @torch._inductor.config.patch(debug=True)
    @fresh_inductor_cache()
    def test_inductor_reduce_scatter_tensor_single(self):
        self._init_process_group()

        def func(arg: torch.Tensor) -> torch.Tensor:
            rs0 = torch.ops._c10d_functional.reduce_scatter_tensor(
                arg, "avg", self.world_size, "default"
            )
            rs0 = torch.ops._c10d_functional.wait_tensor(rs0)
            return rs0

        arg = torch.rand(4, 4, device=self.device)
        compiled = torch.compile(func)
        code = run_and_get_triton_code(compiled, arg)
        (
            FileCheck()
            .check(
                "buf0 = torch.ops._c10d_functional.reduce_scatter_tensor.default(arg0_1"
            )
            .check("torch.ops._c10d_functional.wait_tensor.default(buf0")
            # Expect no extra copy on return
            .check("return (buf0, )")
            .run(code)
        )
        out = compiled(arg)
        correct = func(arg)
        assert same(out, correct), f"{out} va {correct}"

        # Test aoti
        out = AOTIRunnerUtil.run("cuda", func, (arg,))
        torch.cuda.synchronize()

    @unittest.skipIf(not has_triton(), "Inductor+gpu needs triton and recent GPU arch")
    @torch._inductor.config.patch(debug=True)
    @fresh_inductor_cache()
    def test_inductor_reduce_scatter_tensor_coalesced(self):
        self._init_process_group()

        def func(args: List[torch.Tensor]) -> torch.Tensor:
            rs0 = torch.ops._c10d_functional.reduce_scatter_tensor_coalesced(
                args, "avg", self.world_size, "default"
            )
            rs0 = [torch.ops._c10d_functional.wait_tensor(out) for out in rs0]
            return rs0

        args = [torch.rand(4, 4, device=self.device) for _ in range(4)]
        compiled = torch.compile(func)
        code = run_and_get_triton_code(compiled, args)
        (
            FileCheck()
            .check(
                "buf0 = torch.ops._c10d_functional.reduce_scatter_tensor_coalesced"
                ".default([arg0_1, arg1_1, arg2_1, arg3_1]"
            )
            .check("buf1 = buf0[0]")
            .check("buf2 = buf0[1]")
            .check("buf3 = buf0[2]")
            .check("buf4 = buf0[3]")
            .check("torch.ops._c10d_functional.wait_tensor.default(buf1")
            .check("torch.ops._c10d_functional.wait_tensor.default(buf2")
            .check("torch.ops._c10d_functional.wait_tensor.default(buf3")
            .check("torch.ops._c10d_functional.wait_tensor.default(buf4")
            # Expect no extra copy on return
            .check("return (buf1, buf2, buf3, buf4, )")
            .run(code)
        )
        out = compiled(args)
        correct = func(args)
        assert same(out, correct), f"{out} va {correct}"

        # Test aoti
        out = AOTIRunnerUtil.run("cuda", func, (args,))
        torch.cuda.synchronize()

    @unittest.skipIf(not has_triton(), "Inductor+gpu needs triton and recent GPU arch")
    @fresh_inductor_cache()
    def test_inductor_all_to_all_single(self):
        torch._inductor.config.debug = True
        self._init_process_group()
        torch.cuda.set_device(self.device)

        def _tolist_with_constrain_as_size(tensor):
            lst = tensor.tolist()
            for elem in lst:
                torch._constrain_as_size(elem)
            return lst

        def func(
            input: torch.Tensor,
            output_split_sizes: torch.Tensor,
            input_split_sizes: torch.Tensor,
        ) -> torch.Tensor:
            output = torch.ops._c10d_functional.all_to_all_single(
                input,
                _tolist_with_constrain_as_size(output_split_sizes),
                _tolist_with_constrain_as_size(input_split_sizes),
                "default",
            )
            return torch.ops._c10d_functional.wait_tensor(output)

        torch.manual_seed(42)
        send_sz_matrix = torch.randint(0, 20, (self.world_size, self.world_size))

        input_split_sizes = send_sz_matrix[self.rank]
        output_split_sizes = send_sz_matrix[:, self.rank].contiguous()
        input = torch.full((input_split_sizes.sum().item(),), float(self.rank)).cuda()

        with torch._dynamo.config.patch(
            dynamic_shapes=True,
            capture_dynamic_output_shape_ops=True,
            capture_scalar_outputs=True,
        ):
            compiled = torch.compile(func, dynamic=True)
            code = run_and_get_triton_code(
                compiled, input, output_split_sizes, input_split_sizes
            )
        (
            FileCheck()
            .check_regex(
                "torch.ops._c10d_functional.all_to_all_single.default\\("
                "arg\\d+_\\d+, \\[i\\d+, i\\d+\\], \\[i\\d+, i\\d+\\]"
            )
            .check("torch.ops._c10d_functional.wait_tensor.default(")
            .run(code)
        )
        out = compiled(input, output_split_sizes, input_split_sizes)
        correct = func(input, output_split_sizes, input_split_sizes)
        assert same(out, correct), f"{out} va {correct}"


if __name__ == "__main__":
    run_tests()
