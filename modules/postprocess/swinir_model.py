import numpy as np
import torch
from PIL import Image
from rich.progress import Progress, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn, TimeElapsedColumn
from modules.postprocess.swinir_model_arch import SwinIR as net
from modules.postprocess.swinir_model_arch_v2 import Swin2SR as net2
from modules import devices, script_callbacks, shared
from modules.upscaler import Upscaler, compile_upscaler


class UpscalerSwinIR(Upscaler):
    def __init__(self, dirname):
        self.name = "SwinIR"
        self.user_path = dirname
        super().__init__()
        self.scalers = self.find_scalers()
        self.models = {}

    def load_model(self, path, scale=4):
        info = self.find_model(path)
        if info is None:
            return
        if self.models.get(info.local_data_path, None) is not None:
            shared.log.debug(f"Upscaler cached: type={self.name} model={info.local_data_path}")
            return self.models[info.local_data_path]
        pretrained_model = torch.load(info.local_data_path)
        model_v2 = net2(
            upscale=scale,
            in_chans=3,
            img_size=64,
            window_size=8,
            img_range=1.0,
            depths=[6, 6, 6, 6, 6, 6],
            embed_dim=180,
            num_heads=[6, 6, 6, 6, 6, 6],
            mlp_ratio=2,
            upsampler="nearest+conv",
            resi_connection="1conv",
        )
        model_v1 = net(
            upscale=scale,
            in_chans=3,
            img_size=64,
            window_size=8,
            img_range=1.0,
            depths=[6, 6, 6, 6, 6, 6, 6, 6, 6],
            embed_dim=240,
            num_heads=[8, 8, 8, 8, 8, 8, 8, 8, 8],
            mlp_ratio=2,
            upsampler="nearest+conv",
            resi_connection="3conv",
        )
        for model in [model_v1, model_v2]:
            for param in ["params_ema", "params", None]:
                try:
                    if param is not None:
                        model.load_state_dict(pretrained_model[param], strict=True)
                    else:
                        model.load_state_dict(pretrained_model, strict=True)
                    shared.log.info(f"Upscaler loaded: type={self.name} model={info.local_data_path} param={param}")
                    model = compile_upscaler(model)
                    self.models[info.local_data_path] = model
                    return model
                except Exception as e:
                    shared.log.error(f'Upscaler invalid parameters: type={self.name} model={info.local_data_path} {e}')
        return model

    def do_upscale(self, img, selected_model):
        model = self.load_model(selected_model)
        if model is None:
            return img
        model = model.to(devices.device, dtype=devices.dtype)
        img = upscale(img, model)
        if shared.opts.upscaler_unload and selected_model in self.models:
            del self.models[selected_model]
            shared.log.debug(f"Upscaler unloaded: type={self.name} model={selected_model}")
            devices.torch_gc(force=True)
        return img


def upscale(
        img,
        model,
        tile=None,
        tile_overlap=None,
        window_size=8,
        scale=4,
):
    tile = tile or shared.opts.upscaler_tile_size
    tile_overlap = tile_overlap or shared.opts.upscaler_tile_overlap
    img = np.array(img)
    img = img[:, :, ::-1]
    img = np.moveaxis(img, 2, 0) / 255
    img = torch.from_numpy(img).float()
    img = img.unsqueeze(0).to(devices.device, dtype=devices.dtype)
    with torch.no_grad(), devices.autocast():
        _, _, h_old, w_old = img.size()
        h_pad = (h_old // window_size + 1) * window_size - h_old
        w_pad = (w_old // window_size + 1) * window_size - w_old
        img = torch.cat([img, torch.flip(img, [2])], 2)[:, :, : h_old + h_pad, :]
        img = torch.cat([img, torch.flip(img, [3])], 3)[:, :, :, : w_old + w_pad]
        output = inference(img, model, tile, tile_overlap, window_size, scale)
        output = output[..., : h_old * scale, : w_old * scale]
        output = output.data.squeeze().float().cpu().clamp_(0, 1).numpy()
        if output.ndim == 3:
            output = np.transpose(
                output[[2, 1, 0], :, :], (1, 2, 0)
            )  # CHW-RGB to HCW-BGR
        output = (output * 255.0).round().astype(np.uint8)  # float32 to uint8
        return Image.fromarray(output, "RGB")


def inference(img, model, tile, tile_overlap, window_size, scale):
    # test the image tile by tile
    b, c, h, w = img.size()
    tile = min(tile, h, w)
    assert tile % window_size == 0, "tile size should be a multiple of window_size"
    sf = scale
    stride = tile - tile_overlap
    h_idx_list = list(range(0, h - tile, stride)) + [h - tile]
    w_idx_list = list(range(0, w - tile, stride)) + [w - tile]
    E = torch.zeros(b, c, h * sf, w * sf, dtype=devices.dtype, device=devices.device).type_as(img)
    W = torch.zeros_like(E, dtype=devices.dtype, device=devices.device)

    with Progress(TextColumn('[cyan]{task.description}'), BarColumn(), TaskProgressColumn(), TimeRemainingColumn(), TimeElapsedColumn(), console=shared.console) as progress:
        task = progress.add_task(description="Upscaling Initializing", total=len(h_idx_list) * len(w_idx_list))
        for h_idx in h_idx_list:
            if shared.state.interrupted:
                break
            for w_idx in w_idx_list:
                if shared.state.interrupted or shared.state.skipped:
                    break
                in_patch = img[..., h_idx: h_idx + tile, w_idx: w_idx + tile]
                out_patch = model(in_patch)
                out_patch_mask = torch.ones_like(out_patch)

                E[
                ..., h_idx * sf: (h_idx + tile) * sf, w_idx * sf: (w_idx + tile) * sf
                ].add_(out_patch)
                W[
                ..., h_idx * sf: (h_idx + tile) * sf, w_idx * sf: (w_idx + tile) * sf
                ].add_(out_patch_mask)
                progress.update(task, advance=1, description="Upscaling")
    output = E.div_(W)
    return output
