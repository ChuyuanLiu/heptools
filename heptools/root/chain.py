from __future__ import annotations

import bisect
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from logging import Logger
from typing import TYPE_CHECKING, Callable, Generator, Literal, overload

from ..dask.delayed import delayed
from ..system.eos import EOS, PathLike
from ._backend import concat_record, merge_record
from .chunk import Chunk
from .io import TreeReader, TreeWriter
from .merge import resize

if TYPE_CHECKING:
    import awkward as ak
    import dask.array as da
    import dask_awkward as dak
    import numpy as np
    import pandas as pd

    from .io import DelayedRecordLike, RecordLike


@delayed
def _friend_from_merge(
    name: str,
    branches: frozenset[str],
    data: dict[Chunk, list[tuple[int, int, list[Chunk]]]],
    dask: bool
):
    friend = Friend(name)
    friend._branches = frozenset(branches)
    for k, vs in data.items():
        for start, stop, chunks in vs:
            chunks = deque(chunks)
            while chunks:
                chunk = chunks.popleft()
                chunk._branches = friend._branches
                friend._data[k].append(_FriendItem(
                    start, start + len(chunk), chunk))
                start += len(chunk)
            if start != stop:
                raise RuntimeError(
                    f'Failed to merge friend "{name}". The merged chunk does not cover the range [{start},{stop}) for target {k}.')
    return friend


class _FriendItem:
    def __init__(self, start: int, stop: int, chunk: Chunk = None):
        self.start = start
        self.stop = stop
        self.chunk = chunk

    def __lt__(self, other):
        if isinstance(other, _FriendItem):
            return self.stop <= other.start
        return NotImplemented

    def __len__(self):
        return self.stop - self.start

    def __repr__(self):
        return f'[{self.start},{self.stop})'

    def to_json(self):
        return {
            'start': self.start,
            'stop': self.stop,
            'chunk': self.chunk,
        }

    @classmethod
    def from_json(cls, data: dict):
        return cls(data['start'], data['stop'], Chunk.from_json(data['chunk']))


class Friend:
    """
    A tool to create and manage a collection of addtional :class:`TBranch` stored in separate ROOT files. (also known as friend :class:`TTree`)

    Parameters
    ----------
    name : str
        Name of the collection.

    Notes
    -----
    The following special methods are implemented:

    - :meth:`__iadd__` :class:`Friend`
    - :meth:`__add__` :class:`Friend`
    - :meth:`__repr__`
    """
    name: str
    '''str : Name of the collection.'''

    _on_disk_error = 'Cannot perform "{func}()" on friend tree "{name}" when there is in-memory data. Call "dump()" first.'

    def _on_disk(func):
        def wrapper(self, *args, **kwargs):
            if self._to_dump:
                raise RuntimeError(self._on_disk_error.format(
                    func=func.__name__, name=self.name))
            return func(self, *args, **kwargs)
        return wrapper

    def __init__(self, name: str):
        self.name = name
        self._branches: frozenset[str] = None
        self._data: defaultdict[Chunk, list[_FriendItem]] = defaultdict(list)

    @_on_disk
    def __iadd__(self, other) -> Friend:
        if isinstance(other, Friend):
            msg = 'Cannot add friend trees with different {attr}'
            if other._to_dump:
                raise RuntimeError(self._on_disk_error.format(
                    func='__add__', name=other.name))
            if self.name != other.name:
                raise ValueError(
                    msg.format(attr='names'))
            if self._branches != other._branches:
                raise ValueError(
                    msg.format(attr='branches'))
            for k, vs in other._data.items():
                for v in vs:
                    self._insert(k, v)
            return self
        return NotImplemented

    @_on_disk
    def __add__(self, other) -> Friend:
        if isinstance(other, Friend):
            friend = self.copy()
            friend += other
            return friend
        return NotImplemented

    def __repr__(self):
        text = f'{self.name}:{self._branches}'
        for k, v in self._data.items():
            text += f'\n{k}\n    {v}'
        return text

    @classmethod
    def _construct_key(cls, chunk: Chunk):
        return Chunk(
            source=(chunk.path, chunk.uuid),
            name=chunk.name,
            num_entries=None,
            branches=None,
            entry_start=None,
            entry_stop=None)

    @property
    def _to_dump(self):
        if hasattr(self, '_dump'):
            return len(self._dump) > 0
        return False

    def _init_dump(self):
        if not hasattr(self, '_dump'):
            self._dump: list[tuple[Chunk, _FriendItem]] = []
            self._dump_name: dict[Chunk, dict[str, str]] = {}

    def _name_dump(self, target: Chunk, item: _FriendItem):
        if target not in self._dump_name:
            names = {
                'name': self.name,
                'uuid': str(target.uuid),
                'tree': target.name,
            }
            parts = [*target.path.parts]
            if parts[0] in '/\\':
                parts = parts[1:]
            parts[-1] = target.path.stem
            parts = parts[::-1]
            for i in range(len(parts)):
                names[f'path{i}'] = parts[i]
            self._dump_name[target] = names
        return self._dump_name[target] | {'start': item.start, 'stop': item.stop}

    def _check_item(self, item: _FriendItem):
        if len(item.chunk) != len(item):
            raise ValueError(
                f'The number of entries in the chunk {item.chunk} does not match the range {item}')
        if self._branches is None:
            self._branches = frozenset(item.chunk.branches)
        else:
            diff = self._branches - item.chunk.branches
            if diff:
                raise ValueError(
                    f'The chunk {item.chunk} does not have the following branches: {diff}')
        item.chunk = item.chunk.deepcopy(branches=self._branches)

    def _insert(self, target: Chunk, item: _FriendItem):
        series = self._data[target]
        idx = bisect.bisect_left(series, item)
        if idx >= len(series):
            series.append(item)
        else:
            exist = series[idx]
            if item.stop <= exist.start:
                series.insert(idx, item)
            else:
                raise ValueError(
                    f'The new chunk {item} overlaps with the existing one {exist} when inserting into {target}')

    def add(
        self,
        target: Chunk,
        data: RecordLike | Chunk,
    ):
        """
        Create a friend :class:`TTree` for ``target`` using ``data``.

        Parameters
        ----------
        target : Chunk
            A chunk of :class:`TTree`.
        data : RecordLike or Chunk
            Addtional branches added to ``target``.
        """
        item = _FriendItem(target.entry_start, target.entry_stop, data)
        key = self._construct_key(target)
        if not isinstance(data, Chunk):
            self._init_dump()
            self._dump.append((key, item))
        else:
            self._check_item(item)
        self._insert(key, item)

    @overload
    def arrays(self, target: Chunk, filter: Callable[[set[str]], set[str]] = None, library: Literal['ak'] = 'ak', reader_options: dict = None) -> ak.Array:
        ...

    @overload
    def arrays(self, target: Chunk, filter: Callable[[set[str]], set[str]] = None, library: Literal['pd'] = 'pd', reader_options: dict = None) -> pd.DataFrame:
        ...

    @overload
    def arrays(self, target: Chunk, filter: Callable[[set[str]], set[str]] = None, library: Literal['np'] = 'np', reader_options: dict = None) -> dict[str, np.ndarray]:
        ...

    @_on_disk
    def arrays(
        self,
        target: Chunk,
        filter: Callable[[set[str]], set[str]] = None,
        library: Literal['ak', 'pd', 'np'] = 'ak',
        reader_options: dict = None,
    ) -> RecordLike:
        """
        Fetch the friend :class:`TTree` for ``target`` into array.

        Parameters
        ----------
        target : Chunk
            A chunk of :class:`TTree`.
        filter : ~typing.Callable[[set[str]], set[str]], optional
            A function to select branches. If not given, all branches will be read.
        library : ~typing.Literal['ak', 'np', 'pd'], optional, default='ak'
            The library used to represent arrays.
        reader_options : dict, optional
            Additional options passed to :class:`~.io.TreeReader`.

        Returns
        -------
        RecordLike
            Data from friend :class:`TTree`.
        """
        series = self._data[target]
        start = target.entry_start
        stop = target.entry_stop
        chunks = []
        for i in range(bisect.bisect_left(series, _FriendItem(target.entry_start, target.entry_stop)), len(series)):
            if start >= stop:
                break
            item = series[i]
            if item.start > start:
                raise ValueError(
                    f'Friend {self.name} does not have the entries [{start},{item.start}) for {target}')
            else:
                chunk_start = start - item.start
                start = min(stop, item.stop)
                chunk_stop = start - item.start
                chunks.append(item.chunk.slice(chunk_start, chunk_stop))
        branches = self._branches if filter is None else filter(self._branches)
        reader_options = reader_options or {}
        reader_options['filter'] = branches.__and__
        return TreeReader(**reader_options).concat(*chunks, library=library)

    @overload
    def concat(self, *targets: Chunk, filter: Callable[[set[str]], set[str]] = None, library: Literal['ak'] = 'ak', reader_options: dict = None) -> ak.Array:
        ...

    @overload
    def concat(self, *targets: Chunk, filter: Callable[[set[str]], set[str]] = None, library: Literal['pd'] = 'pd', reader_options: dict = None) -> pd.DataFrame:
        ...

    @overload
    def concat(self, *targets: Chunk, filter: Callable[[set[str]], set[str]] = None, library: Literal['np'] = 'np', reader_options: dict = None) -> dict[str, np.ndarray]:
        ...

    @_on_disk
    def concat(
        self,
        *targets: Chunk,
        filter: Callable[[set[str]], set[str]] = None,
        library: Literal['ak', 'pd', 'np'] = 'ak',
        reader_options: dict = None
    ) -> RecordLike:
        """
        Fetch the friend :class:`TTree` for ``targets`` into one array.

        Parameters
        ----------
        targets : tuple[Chunk]
            One or more chunks of :class:`TTree`.
        filter : ~typing.Callable[[set[str]], set[str]], optional
            A function to select branches. If not given, all branches will be read.
        library : ~typing.Literal['ak', 'np', 'pd'], optional, default='ak'
            The library used to represent arrays.
        reader_options : dict, optional
            Additional options passed to :class:`~.io.TreeReader`.

        Returns
        -------
        RecordLike
            Concatenated data.
        """
        if len(targets) == 1:
            return self.arrays(targets[0], filter, library, reader_options)
        else:
            return concat_record(
                [self.arrays(target, filter, library, reader_options)
                 for target in targets],
                library=library)

    @overload
    def dask(self, *targets: Chunk, filter: Callable[[set[str]], set[str]] = None, library: Literal['ak'] = 'ak', reader_options: dict = None) -> dak.Array:
        ...

    @overload
    def dask(self, *targets: Chunk, filter: Callable[[set[str]], set[str]] = None, library: Literal['np'] = 'np', reader_options: dict = None) -> dict[str, da.Array]:
        ...

    @_on_disk
    def dask(
        self,
        *targets: Chunk,
        filter: Callable[[set[str]], set[str]] = None,
        library: Literal['ak', 'np'] = 'ak',
        reader_options: dict = None,
    ) -> DelayedRecordLike:
        """
        Fetch the friend :class:`TTree` for ``targets`` as delayed arrays. The partitions will be preserved.

        Parameters
        ----------
        targets : tuple[Chunk]
            Partitions of target :class:`TTree`.
        filter : ~typing.Callable[[set[str]], set[str]], optional
            A function to select branches. If not given, all branches will be read.
        library : ~typing.Literal['ak', 'np'], optional, default='ak'
            The library used to represent arrays.
        reader_options : dict, optional
            Additional options passed to :class:`~.io.TreeReader`.

        Returns
        -------
        DelayedRecordLike
            Delayed arrays of entries from the friend :class:`TTree`.
        """
        friends = []
        for target in targets:
            series = self._data[target]
            start = target.entry_start
            stop = target.entry_stop
            item = series[bisect.bisect_left(
                series, _FriendItem(target.entry_start, target.entry_stop))]
            if item.start > start:
                raise ValueError(
                    f'Friend {self.name} does not have the entries [{start},{item.start}) for {target}')
            elif item.stop < stop:
                raise ValueError(
                    f'Cannot read one partition from multiple files. Call "merge()" first.')
            else:
                friends.append(item.chunk.slice(
                    start - item.start, stop - item.start))
        branches = self._branches if filter is None else filter(self._branches)
        reader_options = reader_options or {}
        reader_options['filter'] = branches.__and__
        return TreeReader(**reader_options).dask(*friends, library=library)

    def dump(
        self,
        base_path: PathLike = ...,
        naming: str = '{name}_{uuid}_{start}_{stop}.root',
        writer_options: dict = None,
    ):
        """
        Dump all in-memory data to ROOT files with a given ``naming`` rule.

        Parameters
        ----------
        base_path: PathLike, optional
            Base path to store the dumped files. See below for details.
        naming : str, optional
            Naming rule for the dumped files. See below for details.
        writer_options: dict, optional
            Additional options passed to :class:`~.io.TreeWriter`.

        Notes
        -----
        Each dumped file will be stored in ``{base_path}/{naming.format{**keys}}``. If ``base_path`` is not given, the corresponding ``target.path.parent`` will be used. The following keys are available:

        - ``{name}``: :data:`name`.
        - ``{uuid}``: ``target.uuid``
        - ``{tree}``: ``target.name``
        - ``{start}``: ``target.entry_start``
        - ``{stop}``: ``target.entry_stop``
        - ``{path0}``, ``{path1}``, ... : ``target.path.parts`` without suffixes in reversed order.

        where the ``target`` is the one passed to :meth:`add`.

        .. warning::
            The generated path is not guaranteed to be unique. If multiple chunks are dumped to the same path, the last one will overwrite the previous ones.

        Examples
        --------
        The naming rule works as follows:

        .. code-block:: python

            >>> friend = Friend('test')
            >>> friend.add(
            >>>     Chunk(
            >>>         source=('root://host.1//a/b/c/target.root', uuid),
            >>>         name='Events',
            >>>         entry_start=100,
            >>>         entry_stop=200,
            >>>     ),
            >>>     data
            >>> )
            >>> friend.dump(
            >>>     '{name}_{path0}_{uuid}_{tree}_{start}_{stop}.root')
            >>> # write to root://host.1//a/b/c/test_target_uuid_Events_100_200.root
            >>> # or
            >>> friend.dump(
            >>>     '{path2}/{path1}/{name}_{uuid}_{start}_{stop}.root',
            >>>     'root://host.2//x/y/z/')
            >>> # write to root://host.2//x/y/z/b/c/test_uuid_100_200.root
            """
        if self._to_dump:
            if base_path is not ...:
                base_path = EOS(base_path)
            writer_options = writer_options or {}
            writer = TreeWriter(**writer_options)
            for target, item in self._dump:
                if base_path is ...:
                    path = target.path.parent
                else:
                    path = base_path
                path = path / naming.format(**self._name_dump(target, item))
                with writer(path) as f:
                    f.extend(item.chunk)
                item.chunk = f.tree
                self._check_item(item)
            self._dump.clear()

    def reset(self, confirm: bool = True):
        """
        Reset the friend tree and delete all dumped files.

        Parameters
        ----------
        confirm : bool, optional, default=True
            Confirm the deletion.
        """
        files = []
        for vs in self._data.values():
            for v in vs:
                if isinstance(v.chunk, Chunk):
                    files.append(v.chunk.path)
        if confirm:
            Logger.root.info('The following files will be deleted:')
            Logger.root.warning('\n'.join(str(f) for f in files))
            confirmation = input(
                f'Type "{self.name}" to confirm the deletion: ')
            if confirmation != self.name:
                Logger.root.info('Deletion aborted.')
                return
        with ThreadPoolExecutor(max_workers=len(files)) as executor:
            executor.map(EOS.rm, files)
        self._branches = None
        self._data.clear()
        if hasattr(self, '_dump'):
            del self._dump
            del self._dump_name

    @_on_disk
    def merge(
        self,
        step: int,
        chunk_size: int = ...,
        base_path: PathLike = ...,
        naming: str = '{name}_{uuid}.root',
        reader_options: dict = None,
        writer_options: dict = None,
        dask: bool = False,
    ):
        """
        Merge contiguous chunks into a single file.

        Parameters
        ----------
        step : int
            Number of entries to read and write in each iteration step.
        chunk_size : int, optional
            Number of entries in each new chunk. If not given, all entries will be merged into one chunk.
        base_path: PathLike, optional
            Base path to store the merged files. See notes of :meth:`dump` for details.
        naming : str, optional
            Naming rule for the merged files. See notes of :meth:`dump` for details.
        reader_options: dict, optional
            Additional options passed to :class:`~.io.TreeReader`.
        writer_options: dict, optional
            Additional options passed to :class:`~.io.TreeWriter`.
        dask : bool, optional, default=False
            If ``True``, return a :class:`~dask.delayed.Delayed` object.

        Returns
        -------
        Friend or Delayed
            A new friend tree with the merged chunks.
        """
        if base_path is not ...:
            base_path = EOS(base_path)
        self._init_dump()
        data = defaultdict(list)
        for target, items in self._data.items():
            if not items:
                continue
            if base_path is ...:
                base = target.path.parent
            else:
                base = base_path
            start = items[0].start
            items = deque(items)
            to_merge: list[_FriendItem] = []
            while True:
                item = None
                if to_merge:
                    start = to_merge[-1].stop
                if items:
                    item = items.popleft()
                    if start == item.start:
                        to_merge.append(item)
                        continue
                if to_merge:
                    dummy = _FriendItem(to_merge[0].start, to_merge[-1].stop)
                    chunks = [i.chunk for i in to_merge]
                    if len(chunks) > 1:
                        path = base / naming.format(
                            **self._name_dump(target, dummy))
                        chunks = resize(
                            path,
                            *chunks,
                            step=step,
                            chunk_size=chunk_size,
                            writer_options=writer_options,
                            reader_options=reader_options,
                            dask=dask)
                    data[target].append((dummy.start, dummy.stop, chunks))
                    to_merge.clear()
                if item is not None:
                    to_merge.append(item)
                    continue
                if not items:
                    break
        return _friend_from_merge(
            name=self.name,
            branches=self._branches,
            data=dict(data),
            dask=dask)

    @_on_disk
    def clone(
        self,
        base_path: PathLike,
        naming: str = ...,
    ):
        """
        Copy all chunks to a new location.

        Parameters
        ----------
        base_path: PathLike
            Base path to store the cloned files.
        naming : str, optional
            Naming rule for the cloned files. If not given, the original names will be used. See notes of :meth:`dump` for details.

        Returns
        -------
        Friend
            A new friend tree with the cloned chunks.
        """
        base_path = EOS(base_path)
        friend = Friend(self.name)
        friend._branches = self._branches
        src = []
        dst = []
        self._init_dump()
        for target, items in self._data.items():
            if not items:
                continue
            for item in items:
                chunk = item.chunk.deepcopy()
                if naming is ...:
                    name = chunk.path.name
                else:
                    name = naming.format(**self._name_dump(target, item))
                path = base_path / name
                src.append(chunk.path)
                dst.append(path)
                chunk.path = path
                friend._data[target].append(
                    _FriendItem(item.start, item.stop, chunk))
        with ThreadPoolExecutor(max_workers=len(src)) as executor:
            executor.map(
                partial(EOS.cp, parents=True, overwrite=True), src, dst)
        return friend

    def integrity(
        self,
        logger: Logger = None
    ):
        """
        Check and report the following:

        - :meth:`~.chunk.Chunk.integrity` for all target and friend chunks
        - multiple friend chunks from the same source
        - mismatch in number of entries or branches
        - gaps or overlaps between friend chunks
        - in-memory data

        This method can be very expensive for large friend trees.

        Parameters
        ----------
        logger : ~logging.Logger, optional
            The logger used to report the issues. Can be a :class:`~logging.Logger` or any class with the same interface. If not given, the default logger will be used.
        """
        if logger is None:
            logger = Logger.root
        checked: deque[Chunk] = deque()
        files = set()
        for target, items in self._data.items():
            checked.append(target)
            for item in items:
                if isinstance(item.chunk, Chunk):
                    checked.append(item.chunk)
        with ThreadPoolExecutor(max_workers=len(checked)) as executor:
            checked = deque(executor.map(
                partial(Chunk.integrity, logger=logger), checked))
        for target, items in self._data.items():
            target = checked.popleft()
            if target is not None:
                target_name = f'target "{target.path}"\n    '
                start = target.entry_start
                stop = target.entry_stop
                for item in items:
                    if start < item.start:
                        logger.warning(
                            f'{target_name}no friend entries in [{start},{item.start})')
                    elif start > item.start:
                        logger.error(
                            f'{target_name}duplicate friend entries in [{item.start}, {start})')
                    start = item.stop
                    if isinstance(item.chunk, Chunk):
                        chunk = checked.popleft()
                        if chunk is not None:
                            friend_name = f'friend "{chunk.path}"\n    '
                            if len(chunk) != len(item):
                                logger.error(
                                    f'{friend_name}{len(chunk)} entries not fit in [{item.start},{item.stop})')
                            diff = self._branches - chunk.branches
                            if diff:
                                logger.error(
                                    f'{friend_name}missing branches {diff}')
                            if chunk.path in files:
                                logger.error(
                                    f'{friend_name}multiple friend chunks from this source')
                            files.add(chunk.path)
                    else:
                        logger.warning(
                            f'{target_name}in-memory friend entries in [{item.start},{item.stop})')
                if start < stop:
                    logger.warning(
                        f'{target_name}no friend entries in [{start},{stop}) ')
            else:
                for item in items:
                    if isinstance(item.chunk, Chunk):
                        checked.popleft()

    @_on_disk
    def to_json(self):
        """
        Convert ``self`` to JSON data.

        Returns
        -------
        dict
            JSON data.
        """
        return {
            'name': self.name,
            'branches': list(self._branches) if self._branches is not None else self._branches,
            'data': [*self._data.items()],
        }

    @classmethod
    def from_json(cls, data: dict):
        """
        Create :class:`Friend` from JSON data.

        Parameters
        ----------
        data : dict
            JSON data.

        Returns
        -------
        Friend
            A :class:`Friend` object from JSON data.
        """
        friend = cls(data['name'])
        friend._branches = frozenset(data['branches'])
        for k, vs in data['data']:
            items = []
            for v in vs:
                v['chunk']['branches'] = friend._branches
                items.append(_FriendItem.from_json(v))
            friend._data[Chunk.from_json(k)] = items
        return friend

    @_on_disk
    def copy(self):
        """
        Returns
        -------
        Friend
            A shallow copy of ``self``. 
        """
        friend = Friend(self.name)
        friend._branches = self._branches
        for k, vs in self._data.items():
            friend._data[k] = vs.copy()
        return friend


class Chain:
    """
    A :class:`TChain` like object to manage multiple :class:`~.chunk.Chunk` and :class:`Friend`.

    Notes
    -----
    The following special methods are implemented:

    - :meth:`__iadd__` :class:`~.chunk.Chunk`, :class:`Friend`, :class:`Chain`
    - :meth:`__add__` :class:`Chain`
    """

    def __init__(self):
        self._chunks: list[Chunk] = []
        self._friends: dict[str, Friend] = {}

    def add_chunk(self, *chunks: Chunk):
        """
        Add :class:`~.chunk.Chunk` to this chain.

        Parameters
        ----------
        chunks : tuple[Chunk]
            Chunks to add.

        Returns
        -------
        self: Chain
        """
        self._chunks.extend(chunks)
        return self

    def add_friend(self, *friends: Friend):
        """
        Add new :class:`Friend` to this chain or merge to the existing ones.

        Parameters
        ----------
        friends : tuple[Friend]
            Friends to add or merge.

        Returns
        -------
        self: Chain
        """
        for friend in friends:
            name = friend.name
            if name in self._friends:
                self._friends[name] = self._friends[name] + friend
            else:
                self._friends[name] = friend
        return self

    def copy(self):
        """
        Returns
        -------
        Chain
            A shallow copy of ``self``.
        """
        chain = Chain()
        chain._chunks += self._chunks
        chain._friends |= self._friends
        return chain

    def __iadd__(self, other) -> Chain:
        if isinstance(other, Chunk):
            return self.add_chunk(other)
        elif isinstance(other, Friend):
            return self.add_friend(other)
        elif isinstance(other, Chain):
            return (self
                    .add_chunk(*other._chunks)
                    .add_friend(*other._friends.values()))
        else:
            return NotImplemented

    def __add__(self, other) -> Chain:
        if isinstance(other, Chain):
            chain = self.copy()
            chain += other
            return chain
        return NotImplemented

    @overload
    def iterate(self, step: int = ..., library: Literal['ak'] = 'ak', mode: Literal['balance', 'partition'] = 'partition', reader_options: dict = None) -> Generator[ak.Array, None, None]:
        ...

    @overload
    def iterate(self, step: int = ..., library: Literal['pd'] = 'pd', mode: Literal['balance', 'partition'] = 'partition', reader_options: dict = None) -> Generator[pd.DataFrame, None, None]:
        ...

    @overload
    def iterate(self, step: int = ..., library: Literal['np'] = 'np', mode: Literal['balance', 'partition'] = 'partition', reader_options: dict = None) -> Generator[dict[str, np.ndarray], None, None]:
        ...

    def iterate(
        self,
        step: int = ...,
        library: Literal['ak', 'pd', 'np'] = 'ak',
        mode: Literal['balance', 'partition'] = 'partition',
        reader_options: dict = None,
    ) -> Generator[RecordLike, None, None]:
        """
        Iterate over chunks and friend trees.

        Parameters
        ----------
        step : int, optional
            Number of entries to read in each iteration step. If not given, the chunk size will be used and the ``mode`` will be ignored.
        library : ~typing.Literal['ak', 'np', 'pd'], optional, default='ak'
            The library used to represent arrays.
        mode : ~typing.Literal['balance', 'partition'], optional, default='partition'
            The mode to generate iteration steps. See :meth:`~.io.TreeReader.iterate` for details.
        reader_options : dict, optional
            Additional options passed to :class:`~.io.TreeReader`.

        Yields
        ------
        RecordLike
            A chunk of merged data from main and friend :class:`TTree`.
        """
        if step is ...:
            chunks = Chunk.common(*self._chunks)
        elif mode == 'partition':
            chunks = Chunk.partition(step, *self._chunks, common_branches=True)
        elif mode == 'balance':
            chunks = Chunk.balance(step, *self._chunks, common_branches=True)
        else:
            raise ValueError(f'Unknown mode "{mode}"')
        reader_options = reader_options or {}
        reader = TreeReader(**reader_options)
        for chunk in chunks:
            if not isinstance(chunk, list):
                chunk = (chunk,)
            yield merge_record([
                reader.concat(*chunk, library=library),
                *(friend.concat(
                    *chunk,
                    filter=reader_options.get('filter'),
                    library=library,
                    reader_options=reader_options)
                    for friend in self._friends.values())],
                library=library)

    @overload
    def dask(self, partition: int = ..., library: Literal['ak'] = 'ak', reader_options: dict = None) -> tuple[dak.Array, dict[str, dak.Array]]:
        ...

    @overload
    def dask(self, partition: int = ..., library: Literal['np'] = 'np', reader_options: dict = None) -> dict[str, da.Array]:
        ...

    def dask(
        self,
        partition: int = ...,
        library: Literal['ak', 'np'] = 'ak',
        reader_options: dict = None,
    ) -> DelayedRecordLike:
        """
        Read chunks and friend trees into delayed arrays.

        .. todo::
            Merge friend arrays when using ``library='ak'``.

        Parameters
        ----------
        partition: int, optional
            If given, the ``sources`` will be splitted into smaller chunks targeting ``partition`` entries.
        library : ~typing.Literal['ak', 'np'], optional, default='ak'
            The library used to represent arrays.
        reader_options : dict, optional
            Additional options passed to :class:`~.io.TreeReader`.

        Returns
        -------
        main : DelayedRecordLike
            Delayed data from main :class:`TTree`.
        friends : dict[str, DelayedRecordLike], optional
            Delayed data from friend :class:`TTree` with friend names as keys. When using ``library='np'``, the ``friends`` will be merged to ``main``
        """
        if partition is ...:
            partitions = Chunk.common(*self._chunks)
        else:
            partitions = [*Chunk.balance(
                partition, *self._chunks, common_branches=True)]
        reader_options = reader_options or {}
        reader = TreeReader(**reader_options)
        main = reader.dask(*partitions, library=library)
        friends = {
            name: friend.dask(
                *partitions,
                filter=reader_options.get('filter'),
                library=library,
                reader_options=reader_options)
            for name, friend in self._friends.items()}
        if library == 'ak':
            return main, friends
        elif library == 'np':
            return merge_record([main, *friends.values()], library=library)
