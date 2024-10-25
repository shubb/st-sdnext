"""
Lightweight IP-Adapter applied to existing pipeline in Diffusers
- Downloads image_encoder or first usage (2.5GB)
- Introduced via: https://github.com/huggingface/diffusers/pull/5713
- IP adapters: https://huggingface.co/h94/IP-Adapter
TODO ipadapter items:
- SD/SDXL autodetect
"""

import os
import time
import json
from PIL import Image
from modules import processing, shared, devices, sd_models


base_repo = "h94/IP-Adapter"
clip_loaded = None
ADAPTERS = {
    'None': 'none',
    'Base': 'ip-adapter_sd15.safetensors',
    'Base ViT-G': 'ip-adapter_sd15_vit-G.safetensors',
    'Light': 'ip-adapter_sd15_light.safetensors',
    'Plus': 'ip-adapter-plus_sd15.safetensors',
    'Plus Face': 'ip-adapter-plus-face_sd15.safetensors',
    'Full Face': 'ip-adapter-full-face_sd15.safetensors',
    'Base SDXL': 'ip-adapter_sdxl.safetensors',
    'Base ViT-H SDXL': 'ip-adapter_sdxl_vit-h.safetensors',
    'Plus ViT-H SDXL': 'ip-adapter-plus_sdxl_vit-h.safetensors',
    'Plus Face ViT-H SDXL': 'ip-adapter-plus-face_sdxl_vit-h.safetensors',
}


def get_images(input_images):
    output_images = []
    if input_images is None or len(input_images) == 0:
        shared.log.error('IP adapter: no init images')
        return None
    if shared.sd_model_type != 'sd' and shared.sd_model_type != 'sdxl':
        shared.log.error('IP adapter: base model not supported')
        return None
    if isinstance(input_images, str):
        from modules.api.api import decode_base64_to_image
        input_images = decode_base64_to_image(input_images).convert("RGB")
    input_images = input_images.copy()
    if not isinstance(input_images, list):
        input_images = [input_images]
    for image in input_images:
        if image is None:
            continue
        if isinstance(image, list):
            output_images.append(get_images(image)) # recursive
        elif isinstance(image, Image.Image):
            output_images.append(image)
        elif isinstance(image, str):
            from modules.api.api import decode_base64_to_image
            decoded_image = decode_base64_to_image(image).convert("RGB")
            output_images.append(decoded_image)
        elif hasattr(image, 'name'): # gradio gallery entry
            pil_image = Image.open(image.name)
            pil_image.load()
            output_images.append(pil_image)
        else:
            shared.log.error(f'IP adapter: unknown input: {image}')
    return output_images


def get_scales(adapter_scales, adapter_images):
    output_scales = [adapter_scales] if not isinstance(adapter_scales, list) else adapter_scales
    while len(output_scales) < len(adapter_images):
        output_scales.append(output_scales[-1])
    return output_scales


def get_crops(adapter_crops, adapter_images):
    output_crops = [adapter_crops] if not isinstance(adapter_crops, list) else adapter_crops
    while len(output_crops) < len(adapter_images):
        output_crops.append(output_crops[-1])
    return output_crops


def crop_images(images, crops):
    try:
        for i in range(len(images)):
            if crops[i]:
                from shared import yolo # pylint: disable=no-name-in-module
                yolo.load()
                cropped = []
                for image in images[i]:
                    faces = yolo.predict(image)
                    if len(faces) > 0:
                        cropped.append(faces[0].face)
                if len(cropped) == len(images[i]):
                    images[i] = cropped
                else:
                    shared.log.error(f'IP adapter: failed to crop image: source={len(images[i])} faces={len(cropped)}')
    except Exception as e:
        shared.log.error(f'IP adapter: failed to crop image: {e}')
    return images


def unapply(pipe): # pylint: disable=arguments-differ
    try:
        if hasattr(pipe, 'set_ip_adapter_scale'):
            pipe.set_ip_adapter_scale(0)
        if hasattr(pipe, 'unet') and hasattr(pipe.unet, 'config') and pipe.unet.config.encoder_hid_dim_type == 'ip_image_proj':
            pipe.unet.encoder_hid_proj = None
            pipe.config.encoder_hid_dim_type = None
            pipe.unet.set_default_attn_processor()
    except Exception:
        pass


def apply(pipe, p: processing.StableDiffusionProcessing, adapter_names=[], adapter_scales=[1.0], adapter_crops=[False], adapter_starts=[0.0], adapter_ends=[1.0], adapter_images=[]):
    global clip_loaded # pylint: disable=global-statement
    # overrides
    if hasattr(p, 'ip_adapter_names'):
        if isinstance(p.ip_adapter_names, str):
            p.ip_adapter_names = [p.ip_adapter_names]
        adapters = [ADAPTERS.get(adapter, None) for adapter in p.ip_adapter_names if adapter is not None and adapter.lower() != 'none']
        adapter_names = p.ip_adapter_names
    else:
        if isinstance(adapter_names, str):
            adapter_names = [adapter_names]
        adapters = [ADAPTERS.get(adapter, None) for adapter in adapter_names]
    adapters = [adapter for adapter in adapters if adapter is not None and adapter.lower() != 'none']
    if len(adapters) == 0:
        unapply(pipe)
        if hasattr(p, 'ip_adapter_images'):
            del p.ip_adapter_images
        return False
    if shared.sd_model_type != 'sd' and shared.sd_model_type != 'sdxl':
        shared.log.error(f'IP adapter: model={shared.sd_model_type} class={pipe.__class__.__name__} not supported')
        return False
    if hasattr(p, 'ip_adapter_scales'):
        adapter_scales = p.ip_adapter_scales
    if hasattr(p, 'ip_adapter_crops'):
        adapter_crops = p.ip_adapter_crops
    if hasattr(p, 'ip_adapter_starts'):
        adapter_starts = p.ip_adapter_starts
    if hasattr(p, 'ip_adapter_ends'):
        adapter_ends = p.ip_adapter_ends
    if hasattr(p, 'ip_adapter_images'):
        adapter_images = p.ip_adapter_images
    adapter_images = get_images(adapter_images)
    if hasattr(p, 'ip_adapter_masks') and len(p.ip_adapter_masks) > 0:
        adapter_masks = p.ip_adapter_masks
        adapter_masks = get_images(adapter_masks)
    else:
        adapter_masks = []
    if len(adapter_masks) > 0:
        from diffusers.image_processor import IPAdapterMaskProcessor
        mask_processor = IPAdapterMaskProcessor()
        for i in range(len(adapter_masks)):
            adapter_masks[i] = mask_processor.preprocess(adapter_masks[i], height=p.height, width=p.width)
        adapter_masks = mask_processor.preprocess(adapter_masks, height=p.height, width=p.width)
    if len(adapters) < len(adapter_images):
        adapter_images = adapter_images[:len(adapters)]
    if len(adapters) < len(adapter_masks):
        adapter_masks = adapter_masks[:len(adapters)]
    if len(adapter_masks) > 0 and len(adapter_masks) != len(adapter_images):
        shared.log.error('IP adapter: image and mask count mismatch')
        return False
    adapter_scales = get_scales(adapter_scales, adapter_images)
    p.ip_adapter_scales = adapter_scales.copy()
    adapter_crops = get_crops(adapter_crops, adapter_images)
    p.ip_adapter_crops = adapter_crops.copy()
    adapter_starts = get_scales(adapter_starts, adapter_images)
    p.ip_adapter_starts = adapter_starts.copy()
    adapter_ends = get_scales(adapter_ends, adapter_images)
    p.ip_adapter_ends = adapter_ends.copy()
    # init code
    if pipe is None:
        return False
    if not shared.native:
        shared.log.warning('IP adapter: not in diffusers mode')
        return False
    if len(adapter_images) == 0:
        shared.log.error('IP adapter: no image provided')
        adapters = [] # unload adapter if previously loaded as it will cause runtime errors
    if len(adapters) == 0:
        unapply(pipe)
        if hasattr(p, 'ip_adapter_images'):
            del p.ip_adapter_images
        return False
    if not hasattr(pipe, 'load_ip_adapter'):
        shared.log.error(f'IP adapter: pipeline not supported: {pipe.__class__.__name__}')
        return False

    for adapter_name in adapter_names:
        # which clip to use
        if 'ViT' not in adapter_name:
            clip_repo = base_repo
            clip_subfolder = 'models/image_encoder' if shared.sd_model_type == 'sd' else 'sdxl_models/image_encoder' # defaults per model
        elif 'ViT-H' in adapter_name:
            clip_repo = base_repo
            clip_subfolder = 'models/image_encoder' # this is vit-h
        elif 'ViT-G' in adapter_name:
            clip_repo = base_repo
            clip_subfolder = 'sdxl_models/image_encoder' # this is vit-g
        else:
            shared.log.error(f'IP adapter: unknown model type: {adapter_name}')
            return False

        # load feature extractor used by ip adapter
        if pipe.feature_extractor is None:
            from transformers import CLIPImageProcessor
            shared.log.debug('IP adapter load: feature extractor')
            pipe.feature_extractor = CLIPImageProcessor()
        # load image encoder used by ip adapter
        if pipe.image_encoder is None or clip_loaded != f'{clip_repo}/{clip_subfolder}':
            try:
                from transformers import CLIPVisionModelWithProjection
                shared.log.debug(f'IP adapter load: image encoder="{clip_repo}/{clip_subfolder}"')
                pipe.image_encoder = CLIPVisionModelWithProjection.from_pretrained(clip_repo, subfolder=clip_subfolder, torch_dtype=devices.dtype, cache_dir=shared.opts.diffusers_dir, use_safetensors=True)
                clip_loaded = f'{clip_repo}/{clip_subfolder}'
            except Exception as e:
                shared.log.error(f'IP adapter: failed to load image encoder: {e}')
                return False
        sd_models.move_model(pipe.image_encoder, devices.device)

    # main code
    t0 = time.time()
    ip_subfolder = 'models' if shared.sd_model_type == 'sd' else 'sdxl_models'
    try:
        pipe.load_ip_adapter([base_repo], subfolder=[ip_subfolder], weight_name=adapters)
        if hasattr(p, 'ip_adapter_layers'):
            pipe.set_ip_adapter_scale(p.ip_adapter_layers)
            ip_str = ';'.join(adapter_names) + ':' + json.dumps(p.ip_adapter_layers)
        else:
            for i in range(len(adapter_scales)):
                if adapter_starts[i] > 0:
                    adapter_scales[i] = 0.00
            pipe.set_ip_adapter_scale(adapter_scales)
            ip_str =  [f'{os.path.splitext(adapter)[0]}:{scale}:{start}:{end}' for adapter, scale, start, end in zip(adapter_names, adapter_scales, adapter_starts, adapter_ends)]
        p.task_args['ip_adapter_image'] = crop_images(adapter_images, adapter_crops)
        if len(adapter_masks) > 0:
            p.cross_attention_kwargs = { 'ip_adapter_masks': adapter_masks }
        p.extra_generation_params["IP Adapter"] = ';'.join(ip_str)
        t1 = time.time()
        shared.log.info(f'IP adapter: {ip_str} image={adapter_images} mask={adapter_masks is not None} time={t1-t0:.2f}')
    except Exception as e:
        shared.log.error(f'IP adapter failed to load: repo="{base_repo}" folder="{ip_subfolder}" weights={adapters} names={adapter_names} {e}')
    return True
