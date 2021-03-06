# Copyright 2016 Quora, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

__doc__ = """

Helper functions for use with asynq (similar to itertools).

"""

from .contexts import AsyncContext
from .decorators import async, async_proxy, async_call, AsyncDecorator, AsyncDecoratorBinder
from .futures import ConstFuture
# we shouldn't use the return syntax in generators here so that asynq can be imported
# under Python versions that lack our patch to allow returning from generators
from .utils import result

from qcore import get_original_fn, utime
from qcore.caching import get_args_tuple, get_kwargs_defaults
from qcore.inspection import getargspec
from qcore.events import EventHook
from qcore.errors import reraise, prepare_for_reraise
from qcore.decorators import decorate
import functools
import itertools
import weakref
import threading


@async()
def amap(function, sequence):
    """Equivalent of map() that takes an async map function.

    Returns a list.

    """
    result((yield [function.async(elt) for elt in sequence])); return


@async()
def afilter(function, sequence):
    """Equivalent of filter() that takes an async filter function.

    Returns a list.

    """
    if function is None:
        result(filter(None, sequence)); return
    should_include = yield [function.async(elt) for elt in sequence]
    result(list(itertools.compress(sequence, should_include))); return


@async()
def afilterfalse(function, sequence):
    """Equivalent of itertools.ifilterfalse() that takes an async filter function.

    Returns a list.

    """
    should_exclude = yield [function.async(elt) for elt in sequence]
    should_include = [not res for res in should_exclude]
    result(list(itertools.compress(sequence, should_include))); return


@async()
def asorted(iterable, key=None, reverse=False):
    """Equivalent of sorted() that takes an async key function.

    The cmp= argument of sorted() is not supported.

    Returns a list.

    """
    values = list(iterable)
    if key is None:
        keys = values
    else:
        keys = yield amap.async(key, values)
    # we need to use key= here because otherwise we will compare the values when the key are
    # equal, which would be a behavior difference between sorted() and asorted()
    pairs = sorted(zip(keys, values), key=lambda p: p[0], reverse=reverse)
    result([p[1] for p in pairs]); return


@async()
def amax(*args, **kwargs):
    """Async equivalent of max()."""
    key_fn = kwargs.pop('key', None)
    if kwargs:
        raise TypeError('amax() got an unexpected keyword argument')

    if len(args) == 0:
        raise TypeError('amax() expected 1 arguments, got 0')
    elif len(args) == 1:
        iterable = args[0]
    else:
        iterable = args

    if key_fn is None:
        result(max(iterable)); return

    # support generators
    if not isinstance(iterable, (list, tuple)):
        iterable = list(iterable)

    keys = yield amap.async(key_fn, iterable)
    max_pair = max(enumerate(iterable), key=lambda pair: keys[pair[0]])
    result(max_pair[1]); return


@async()
def amin(*args, **kwargs):
    """Async equivalent of min()."""
    key_fn = kwargs.pop('key', None)
    if kwargs:
        raise TypeError('amin() got an unexpected keyword argument')

    if len(args) == 0:
        raise TypeError('amin() expected 1 arguments, got 0')
    elif len(args) == 1:
        iterable = args[0]
    else:
        iterable = args

    if key_fn is None:
        result(min(iterable)); return

    # support generators
    if not isinstance(iterable, (list, tuple)):
        iterable = list(iterable)

    keys = yield amap.async(key_fn, iterable)
    max_pair = min(enumerate(iterable), key=lambda pair: keys[pair[0]])
    result(max_pair[1]); return


@async()
def asift(pred, items):
    """Sifts a list of items into those that meet the predicate and those that don't."""
    yes = []
    no = []
    results = yield [pred.async(item) for item in items]
    for item, yesno in zip(items, results):
        if yesno:
            yes.append(item)
        else:
            no.append(item)
    result((yes, no)); return


def acached_per_instance():
    """Async equivalent of core.caching.cached_per_instance().

    Unlike l0cache, the cached value is stored in the instance so that it gets
    garbage collected together with the instance.

    The cached values are not stored when the object is pickled.

    """
    def cache_fun(fun):
        argspec = getargspec(get_original_fn(fun))
        arg_names = argspec.args[1:]  # remove self
        async_fun = fun.async
        kwargs_defaults = get_kwargs_defaults(argspec)
        cache = {}

        def cache_key(args, kwargs):
            return get_args_tuple(args, kwargs, arg_names, kwargs_defaults)

        def clear_cache(instance_key, ref):
            del cache[instance_key]

        @async_proxy()
        @functools.wraps(fun)
        def new_fun(self, *args, **kwargs):
            instance_key = id(self)
            if instance_key not in cache:
                ref = weakref.ref(self, functools.partial(clear_cache, instance_key))
                cache[instance_key] = (ref, {})
            instance_cache = cache[instance_key][1]

            k = cache_key(args, kwargs)
            try:
                return ConstFuture(instance_cache[k])
            except KeyError:
                def callback(task):
                    instance_cache[k] = task.value()

                task = async_fun(self, *args, **kwargs)
                task.on_computed.subscribe(callback)
                return task

        # just so unit tests can check that this is cleaned up correctly
        new_fun.__acached_per_instance_cache__ = cache
        return new_fun
    return cache_fun


@async()
def call_with_context(context, fn, *args, **kwargs):
    """Calls fn in the given with context.

    This is useful if you need to call two functions at once, but only one should be called in the
    context. For example:

        important, not_important = yield (
            get_important.async(oid),
            call_with_context.async(a.livenode.dep.IgnoreDependencies(), get_not_important, oid).
        )

    """
    with context:
        result((yield fn.async(*args, **kwargs))); return


class DeduplicateDecoratorBinder(AsyncDecoratorBinder):
    def dirty(self, *args, **kwargs):
        if self.instance is None:
            self.decorator.dirty(*args, **kwargs)
        else:
            self.decorator.dirty(self.instance, *args, **kwargs)


class DeduplicateDecorator(AsyncDecorator):
    binder_cls = DeduplicateDecoratorBinder
    tasks = {}

    def __init__(self, fn, task_cls, keygetter):
        AsyncDecorator.__init__(self, fn, task_cls)
        self.keygetter = keygetter

    def cache_key(self, args, kwargs):
        return self.keygetter(args, kwargs), threading.current_thread()

    def async(self, *args, **kwargs):
        cache_key = self.cache_key(args, kwargs)

        try:
            return self.tasks[cache_key]
        except KeyError:
            task = self.fn.async(*args, **kwargs)

            def callback(task):
                del self.tasks[cache_key]

            self.tasks[cache_key] = task
            task.on_computed.subscribe(callback)
            return task

    def dirty(self, *args, **kwargs):
        cache_key = self.cache_key(args, kwargs)
        self.tasks.pop(cache_key, None)


def deduplicate(keygetter=None):
    """Decorator that ensures that no two identical instances of a task run concurrently.

    This is useful in situations like this:

        @async()
        def should_filter_object(oid, uid):
            data = yield get_data_for_user.async(uid)
            ...

        @async()
        def filter_objects(oids, uid):
            ... = yield [should_filter_object.async(oid, uid) for oid in oids]

    where get_data_for_user is cached (e.g. in memcache or l0cache). Without the deduplicate
    decorator, this may end up calling the body of the get_data_for_user function multiple times,
    despite the caching, because a second async task may enter the body while the first one is
    still active.

    You can also call dirty on a deduplicated function to remove a cached async task with the
    corresponding args and kwargs. This is useful if a deduplicating function ends up calling
    itself with the same args and kwargs, either directly or deeper in the call stack.

    """
    def decorator(fun):
        _keygetter = keygetter
        if _keygetter is None:
            original_fn = get_original_fn(fun)
            argspec = getargspec(original_fn)
            arg_names = argspec.args
            kwargs_defaults = get_kwargs_defaults(argspec)
            _keygetter = lambda args, kwargs: get_args_tuple(args, kwargs, arg_names, kwargs_defaults)

        return decorate(DeduplicateDecorator, fun.task_cls, _keygetter)(fun)
    return decorator


class AsyncTimer(AsyncContext):
    """Simple async-aware timer class.

    Use this to find out how long a block of code takes within an async task. If
    other tasks run interspersed with the task in which this is used, time spent
    executing those tasks will not be counted. The result (in microseconds) will
    be available as the total_time attribute on the context object after exiting
    the context.

    The total_time attribute may have a nonzero value during the context if any
    yields were performed. Because of optimizations that may be done in the
    future to how contexts work between tasks, the value shouldn't be trusted
    until exiting the context.

    Usage example:
        @async()
        def potentially_slow_function(x):

            with AsyncTimer() as t:
                yield do_a_lot_of_work.async(x)
                # don't use t.total_time here!

            report_time_for_x(x, t.total_time)

        yield [potentially_slow_function(x) for x in all_x_values]

    """

    def __init__(self):
        self.total_time = 0
        self._last_start_time = None

    def resume(self):
        self._last_start_time = utime()

    def pause(self):
        self.total_time += utime() - self._last_start_time


class AsyncEventHook(EventHook):
    """EventHook that supports async handlers.

    When the event triggers, all the async handlers will be invoked asynchronously.

    All non-async handlers will be invoked normally (same as EventHook).

    """

    @async()
    def trigger(self, *args):
        yield [async_call.async(handler, *args) for handler in self]

    @async()
    def safe_trigger(self, *args):
        wrapped_handlers = [self._create_safe_wrapper(handler) for handler in self]
        results = yield [wrapped_handler.async(*args) for wrapped_handler in wrapped_handlers]
        for error in filter(None, results):
            reraise(error)

    @staticmethod
    def _create_safe_wrapper(handler):
        @async()
        def wrapped(*args):
            error = None
            try:
                yield async_call.async(handler, *args)
            except BaseException as e:
                prepare_for_reraise(e)
                error = e
            result(error); return
        return wrapped
