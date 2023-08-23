from __future__ import annotations

import re
from typing import Callable, Generator, Generic, TypeVar

from ..utils import match_any, merge_op, unpack

_LeafType = TypeVar('_LeafType')
class Tree(dict[str], Generic[_LeafType]):
    def __init__(self, leaf: Callable[[], _LeafType] = None):
        self.leaf = leaf

    def __getitem__(self, __key) -> Tree[_LeafType] | _LeafType:
        if __key is None:
            return self
        if isinstance(__key, tuple):
            if len(__key) == 0:
                return self
            if __key[0] not in self:
                super().__setitem__(__key[0], Tree[_LeafType](self.leaf))
            return self[__key[0]][unpack(__key[1:])]
        elif isinstance(__key, str):
            if __key not in self:
                super().__setitem__(__key, self.leaf() if self.leaf else None)
            return super().__getitem__(__key)

    def __setitem__(self, __key, __value) -> Tree[_LeafType] | _LeafType:
        if isinstance(__key, tuple):
            self[(__key[0], )][unpack(__key[1:])] = __value
        elif isinstance(__key, str):
            super().__setitem__(__key, __value)

    def walk(self, *pattern: list[str | list[str]]) -> Generator[tuple[tuple[str, ...], _LeafType]]:
        for k in self.keys():
            if not pattern or match_any(k, pattern[0], lambda x, y: re.match(y, x) is not None):
                if isinstance(self[k], Tree):
                    for meta, entry in self[k].walk(*(pattern[1:] if len(pattern) > 1 else [])):
                        yield (k, *meta), entry
                else:
                    yield (k,), self[k]

    def __str__(self): # TODO rich, __repr__
        lines = []
        branches = sorted(self.keys())
        for i, k in enumerate(branches):
            if i == len(branches) - 1:
                lines.append((f' └─{k}\n' + str(self[k])).replace('\n', '\n  '))
            else:
                lines.append((f' ├─{k}\n' + str(self[k])).replace('\n', '\n │'))
        return '\n'.join(lines)

    def iop(self, other: Tree[_LeafType], op: Callable[[_LeafType, _LeafType], _LeafType]) -> Tree[_LeafType]:
        if isinstance(other, Tree):
            if not self.leaf is other.leaf:
                raise TypeError(f'cannot operate on trees with different leaf types: "{self.leaf}" and "{other.leaf}"')
            for k, v in other.items():
                if isinstance(v, Tree):
                    self[(k, )].iop(v, op)
                else:
                    if k in self:
                        self[k] = merge_op(op, self[k], v)
                    else:
                        self[k] = v
            return self
        return NotImplemented

    @staticmethod
    def op(first: Tree[_LeafType], second: Tree[_LeafType], op: Callable[[_LeafType, _LeafType], _LeafType]) -> Tree[_LeafType]:
        if isinstance(first, Tree) and isinstance(second, Tree):
            tree = Tree[_LeafType](first.leaf)
            tree.iop(first, op)
            tree.iop(second, op)
            return tree
        return NotImplemented

    def __ior__(self, other: Tree[_LeafType]) -> Tree[_LeafType]:
        return self.iop(other, lambda x, y: x | y)

    def __or__(self, other: Tree[_LeafType]) -> Tree[_LeafType]:
        return self.op(self, other, lambda x, y: x | y)

    def __iadd__(self, other: Tree[_LeafType]) -> Tree[_LeafType]:
        return self.iop(other, lambda x, y: x + y)

    def __add__(self, other: Tree[_LeafType]) -> Tree[_LeafType]:
        return self.op(self, other, lambda x, y: x + y)

    def from_dict(self, tree: dict, depth: int = float('inf')):
        for k, v in tree.items():
            if isinstance(v, dict) and depth > 1:
                self[(k, )].from_dict(v, depth = depth - 1)
            else:
                self[k] = self.leaf(v) if self.leaf else v
        return self