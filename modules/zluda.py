import sys
from typing import Union
import torch
from torch._prims_common import DeviceLikeType
import onnxruntime as ort
from modules import shared, devices
from modules.onnx_impl.execution_providers import available_execution_providers, ExecutionProvider
from modules.zluda_hijacks import do_hijack


PLATFORM = sys.platform
do_nothing = lambda _: None # pylint: disable=unnecessary-lambda-assignment


def test(device: DeviceLikeType) -> Union[Exception, None]:
    device = torch.device(device)
    try:
        ten1 = torch.randn((2, 4,), device=device)
        ten2 = torch.randn((4, 8,), device=device)
        out = torch.mm(ten1, ten2)
        assert out.sum().is_nonzero()
        return None
    except Exception as e:
        return e


def initialize_zluda():
    shared.cmd_opts.device_id = None
    device = devices.get_optimal_device()
    if not devices.cuda_ok or not devices.has_zluda():
        return

    do_hijack()

    if PLATFORM == "win32":
        torch.backends.cudnn.enabled = False
        torch.backends.cuda.enable_flash_sdp(False)
        torch.backends.cuda.enable_flash_sdp = do_nothing
        torch.backends.cuda.enable_math_sdp(True)
        torch.backends.cuda.enable_math_sdp = do_nothing
        torch.backends.cuda.enable_mem_efficient_sdp(False)
        torch.backends.cuda.enable_mem_efficient_sdp = do_nothing
        if hasattr(torch.backends.cuda, "enable_cudnn_sdp"):
            torch.backends.cuda.enable_cudnn_sdp(False)
            torch.backends.cuda.enable_cudnn_sdp = do_nothing

        # ONNX Runtime is not supported
        ort.capi._pybind_state.get_available_providers = lambda: [v for v in available_execution_providers if v != ExecutionProvider.CUDA] # pylint: disable=protected-access
        ort.get_available_providers = ort.capi._pybind_state.get_available_providers # pylint: disable=protected-access
        if shared.opts.onnx_execution_provider == ExecutionProvider.CUDA:
            shared.opts.onnx_execution_provider = ExecutionProvider.CPU

        result = test(device)
        if result is not None:
            shared.log.warning(f'ZLUDA device failed to pass basic operation test: index={device.index}, device_name={torch.cuda.get_device_name(device)}')
            shared.log.error(result)
            torch.cuda.is_available = lambda: False
            devices.cuda_ok = False
            devices.backend = 'cpu'
            devices.device = devices.cpu
