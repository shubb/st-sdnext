import os
from datetime import datetime
import torch
from modules import shared, sd_models


class Module():
    name: str = ''
    cls: str = None
    device: str = None
    dtype: str = None
    params: int = 0
    modules: int = 0
    config: dict = None

    def __init__(self, name, module):
        self.name = name
        self.cls = module.__class__.__name__
        if isinstance(module, tuple):
            self.cls = module[1]
        if hasattr(module, 'config'):
            self.config = module.config
        if isinstance(module, torch.nn.Module):
            self.device = getattr(module, 'device', None)
            self.dtype = getattr(module, 'dtype', None)
            self.params = sum(p.numel() for p in module.parameters(recurse=True))
            self.modules = len(list(module.modules()))

    def __repr__(self):
        s = f'name="{self.name}" cls={self.cls} config={self.config is not None}'
        if self.device or self.dtype:
            s += f' device={self.device} dtype={self.dtype}'
        if self.params or self.modules:
            s += f' params={self.params} modules={self.modules}'
        return s


class Model():
    name: str = ''
    fn: str = ''
    type: str = ''
    cls: str = ''
    hash: str = ''
    meta: dict = {}
    size: int = 0
    mtime: datetime = None
    info: sd_models.CheckpointInfo = None
    modules: list[Module] = []

    def __init__(self, name):
        self.name = name
        if not shared.sd_loaded:
            return
        self.cls = shared.sd_model.__class__.__name__
        self.type = shared.sd_model_type
        self.info = sd_models.get_closet_checkpoint_match(name)
        if self.info is not None:
            self.name = self.info.name or self.name
            self.hash = self.info.shorthash or ''
            self.meta = self.info.metadata or {}
            if os.path.exists(self.info.filename):
                stat = os.stat(self.info.filename)
                self.mtime = datetime.fromtimestamp(stat.st_mtime).replace(microsecond=0)
                if os.path.isfile(self.info.filename):
                    self.size = round(stat.st_size)

    def __repr__(self):
        return f'model="{self.name}" type={self.type} class={self.cls} size={self.size} mtime="{self.mtime}" modules={self.modules}'


def analyze():
    model = Model(shared.opts.sd_model_checkpoint)
    if model.cls == '':
        return model
    if hasattr(shared.sd_model, '_internal_dict'):
        keys = shared.sd_model._internal_dict.keys() # pylint: disable=protected-access
    else:
        keys = sd_models.get_signature(shared.sd_model).keys()
    model.modules.clear()
    for k in keys: # pylint: disable=protected-access
        component = getattr(shared.sd_model, k, None)
        module = Module(k, component)
        model.modules.append(module)
    shared.log.debug(f'Analyzed: {model}')
    return model
