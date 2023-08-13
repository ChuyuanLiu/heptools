from __future__ import annotations

import re
from typing import (Any, Callable, Generic, Iterable, Sized, TypeVar, Union,
                    get_args, get_origin)

__all__ = ['isinstance_', 'sequence_call', 'astuple']

def isinstance_(__obj, __class_or_tuple) -> bool:
    if __class_or_tuple is Any:
        return True
    origin = get_origin(__class_or_tuple)
    if origin is Union:
        return isinstance(__obj, get_args(__class_or_tuple))
    elif origin is type:
        if not isinstance(__obj, type):
            return False
        return issubclass(__obj, get_args(__class_or_tuple))
    elif origin is None:
        return isinstance(__obj, __class_or_tuple)
    else:
        return isinstance(__obj, origin)

def type_name(__type) -> str:
    if isinstance(__type, Iterable):
        return f'({", ".join(type_name(i) for i in __type)})'
    origin = get_origin(__type)
    args = get_args(__type)
    is_callable = len(args) == 2 and isinstance(args[0], list)
    args = [type_name(arg) for arg in args]
    if not origin and not args:
        if __type is Any:
            return 'Any'
        if __type is Ellipsis:
            return '...'
        if __type is None or __type is type(None):
            return 'None'
        return __type.__name__
    if is_callable:
        return f'{args[0]} -> {args[1]}'
    if origin is Union:
        return ' | '.join(args)
    if origin is type:
        if len(args) != 1:
            raise TypeError(f'invalid type <{__type}>')
        return f'<{args[0]}>'
    if args:
        args = f'[{", ".join(args)}]'
    return f'{type_name(origin)}{args}'

def sequence_call(*_funcs: Callable[[Any], Any]):
    def func(x):
        for _func in _funcs:
            x = _func(x)
        return x
    return func

def astuple(_o):
    return _o if isinstance(_o, tuple) else (_o,)

def unpack(__iter: Iterable) -> Any:
    __next = __iter
    while isinstance(__next, Iterable) and isinstance(__next, Sized) and not isinstance(__next, str):
        if len(__next) == 1:
            __next = __next[0]
        else:
            return __iter
    return __next

_TargetType = TypeVar('_TargetType')
_PatternType = TypeVar('_PatternType')
def match_any(target: _TargetType, patterns: Iterable[_PatternType], match: Callable[[_TargetType, _PatternType], bool]):
    if patterns in [None, ...]:
        return True
    if not isinstance(patterns, Iterable) or isinstance(patterns, str):
        patterns = [patterns]
    for pattern in patterns:
        if match(target, pattern):
            return True
    return False

_EvalType = TypeVar('_EvalType')
class Eval(Generic[_EvalType]):
    _quote_arg_pattern = re.compile(r'(?P<arg>' +
                                    r'|'.join([rf'((?<={i})[^\[\]\",=]*?(?={j}))'
                                               for i in ['\[', ',']
                                               for j in [',', '\]']]) +
                                    r')')
    _eval_call_pattern = re.compile(r'\[(?P<arg>.*?)\]')

    def __init__(self, method: Callable[[], _EvalType] | dict[str, _EvalType], *args, **kwargs):
        self.method = method
        self.args   = args
        self.kwargs = kwargs

    def __call__(self, expression: str) -> _EvalType:
        return eval(re.sub(self._eval_call_pattern, rf'self.method(\g<arg>,*self.args,**self.kwargs)', re.sub(self._quote_arg_pattern, r'"\g<arg>"', expression)))

    def __getitem__(self, expression: str) -> _EvalType:
        return eval(re.sub(self._eval_call_pattern, rf'self.method[\g<arg>]', re.sub(self._quote_arg_pattern, r'"\g<arg>"', expression)))