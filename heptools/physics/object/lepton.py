from functools import partial

from . import vector as vec
from .utils import register_behavior

__all__ = ['pair']

@register_behavior
class MultiLepton(vec.MultiLorentzVector):
    @property
    def charge(self):
        return self._p1.charge + self._p2.charge

pair = partial(vec.pair, name = 'MultiLepton')