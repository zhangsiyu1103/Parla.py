import abc
import logging
import random
from abc import abstractmethod, ABCMeta
from collections import deque, namedtuple
from contextlib import contextmanager
import threading
import time
from numbers import Number
from threading import Thread, Condition
from typing import Optional, Collection, Union, Dict, List, Any, Tuple

from .device import get_all_devices, Device, Architecture

logger = logging.getLogger(__name__)

__all__ = ["Task", "SchedulerContext", "DeviceSetRequirements", "OptionsRequirements", "ResourceRequirements"]


_ASSIGNMENT_FAILURE_WARNING_LIMIT = 32


# Note: tasks can be implemented as lock free, however, atomics aren't really a thing in Python, so instead
# make each task have its own lock to mimic atomic-like counters for dependency tracking.


TaskAwaitTasks = namedtuple("AwaitTasks", ("dependencies", "value_task"))


class TaskState(object, metaclass=abc.ABCMeta):
    __slots__ = []

    @property
    @abstractmethod
    def is_terminal(self):
        raise NotImplementedError()


class TaskRunning(TaskState):
    __slots__ = ["func", "args", "dependencies"]

    @property
    def is_terminal(self):
        return False

    def __init__(self, func, args, dependencies):
        if dependencies is not None and (
                not isinstance(dependencies, Collection) or not all(isinstance(d, Task) for d in dependencies)):
            raise ValueError("dependencies must be a collection of Tasks")
        self.dependencies = dependencies
        self.args = args
        self.func = func

    def clear_dependencies(self):
        self.dependencies = None

    def __repr__(self):
        return "TaskRunning({}, {}, {})".format(self.func.__name__, self.args, self.dependencies)


class TaskCompleted(TaskState):
    __slots__ = ["ret"]

    def __init__(self, ret):
        self.ret = ret

    @property
    def is_terminal(self):
        return True

    def __repr__(self):
        return "TaskCompleted({})".format(self.ret)


class TaskException(TaskState):
    __slots__ = ["exc"]

    @property
    def is_terminal(self):
        return True

    def __init__(self, exc):
        self.exc = exc

    def __repr__(self):
        return "TaskException({})".format(self.exc)


ResourceDict = Dict[str, Union[float, int]]

class ResourceRequirements(object, metaclass=abc.ABCMeta):
    __slots__ = ["resources", "ndevices"]

    resources: ResourceDict
    ndevices: int

    def __init__(self, resources: ResourceDict, ndevices):
        self.resources = resources
        assert all(isinstance(v, str) for v in resources.keys())
        assert all(isinstance(v, (float, int)) for v in resources.values())
        self.ndevices = ndevices
        # if "memory" not in resources:
        #     logger.info(
        #         "memory resource not provided in device request for %r (add memory=x where x is the bytes used)",
        #         architecture_or_device)

    @property
    def exact(self):
        return False


class DeviceSetRequirements(ResourceRequirements):
    __slots__ = ["devices"]
    devices: List[Device]

    def __init__(self, resources: ResourceDict, ndevices: int, devices: List[Device]):
        super().__init__(resources, ndevices)
        assert devices
        assert all(isinstance(dd, Device) for dd in devices)
        self.devices = list(devices)

    @property
    def exact(self):
        assert len(self.devices) >= self.ndevices
        return len(self.devices) == self.ndevices


class OptionsRequirements(ResourceRequirements):
    __slots__ = ["options"]
    options: List[List[Device]]

    def __init__(self, resources, ndevices, options):
        super().__init__(resources, ndevices)
        assert len(options) > 1
        assert all(isinstance(a, Device) for a in options)
        self.options = options

    @property
    def possibilities(self):
        return (DeviceSetRequirements(self.resources, self.ndevices, ds) for ds in self.options)


class Task:
    assigned: bool
    req: ResourceRequirements
    _dependees: List["Task"]
    _state: TaskState

    def __init__(self, func, args, dependencies: Collection["Task"], taskid,
                 req: ResourceRequirements):
        self._mutex = threading.Lock()
        with self._mutex:
            self.taskid = taskid

            self.req = req
            self.assigned = False

            self._state = TaskRunning(func, args, None)
            self._dependees = []

            get_scheduler_context().incr_active_tasks()

            self._set_dependencies(dependencies)

            # Expose the self reference to other threads as late as possible, but not after potentially getting
            # scheduled.
            taskid.task = self
            
            logger.debug("Task %r: Creating", self)

            self._check_remaining_dependencies()

    def _set_dependencies(self, dependencies):
        self._remaining_dependencies = len(dependencies)
        for dep in dependencies:
            if not dep._add_dependee(self):
                self._remaining_dependencies -= 1

    @property
    def result(self):
        if isinstance(self._state, TaskCompleted):
            return self._state.ret
        elif isinstance(self._state, TaskException):
            raise self._state.exc

    def _complete_dependency(self):
        with self._mutex:
            self._remaining_dependencies -= 1
            self._check_remaining_dependencies()

    def _check_remaining_dependencies(self):
        if not self._remaining_dependencies:
            logger.info("Task %r: Scheduling", self)
            get_scheduler_context().enqueue_task(self)

    def _add_dependee(self, dependee: "Task"):
        """Add the dependee if self is not completed, otherwise return False."""
        with self._mutex:
            if self._state.is_terminal:
                return False
            else:
                self._dependees.append(dependee)
                return True

    def run(self):
        ctx = get_scheduler_context()
        task_state = TaskException(RuntimeError("Unknown fatal error"))
        assert isinstance(self.req, DeviceSetRequirements) and self.req.exact, "Task was not assigned before running."
        for d in self.req.devices:
            ctx.scheduler._available_resources.allocate_resources(d, self.req.resources, blocking=True)
        try:
            with _scheduler_locals._devices_scope(self.req.devices):
                try:
                    assert isinstance(self._state, TaskRunning)
                    task_state = self._state.func(self, *self._state.args)
                    if task_state is None:
                        task_state = TaskCompleted(None)
                except Exception as e:
                    task_state = TaskException(e)
                finally:
                    logger.info("Finally for task %r", self)
                    for d in self.req.devices:
                        ctx.scheduler._available_resources.deallocate_resources(d, self.req.resources)
                        ctx.scheduler._unassigned_resources.deallocate_resources(d, self.req.resources)
                    self._set_state(task_state)
        except Exception as e:
            logger.exception("Task %r: Exception in task handling", self)
            raise e

    def _notify_dependees(self):
        with self._mutex:
            for dependee in self._dependees:
                dependee._complete_dependency()

    def __await__(self):
        return (yield TaskAwaitTasks([self], self))

    def __repr__(self):
        return "<Task nrem_deps={_remaining_dependencies} state={_state}>".format(**self.__dict__)

    def _set_state(self, new_state: TaskState):
        # old_state = self._state
        logger.info("Task %r: %r -> %r", self, self._state, new_state)
        self._state = new_state
        ctx = get_scheduler_context()

        if isinstance(new_state, TaskException):
            ctx.scheduler.report_exception(new_state.exc)
        elif isinstance(new_state, TaskRunning):
            self._set_dependencies(new_state.dependencies)
            self._check_remaining_dependencies()
            new_state.clear_dependencies()

        if new_state.is_terminal:
            self._notify_dependees()
            ctx.decr_active_tasks()


class InvalidSchedulerAccessException(RuntimeError):
    pass


class SchedulerContext(metaclass=ABCMeta):
    def spawn_task(self, function, args, deps, taskid, req):
        return Task(function, args, deps, taskid, req)

    @abstractmethod
    def enqueue_task(self, task):
        raise NotImplementedError()

    def __enter__(self):
        _scheduler_locals._scheduler_context_stack.append(self)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        _scheduler_locals._scheduler_context_stack.pop()

    @property
    @abstractmethod
    def scheduler(self) -> "Scheduler":
        raise NotImplementedError()

    @abstractmethod
    def incr_active_tasks(self):
        raise NotImplementedError()

    @abstractmethod
    def decr_active_tasks(self):
        raise NotImplementedError()


class _SchedulerLocals(threading.local):
    def __init__(self):
        super(_SchedulerLocals, self).__init__()
        self._scheduler_context_stack = []

    @property
    def devices(self):
        if hasattr(self, "_devices"):
            return self._devices
        else:
            raise InvalidSchedulerAccessException("Devices not set in this context")

    @contextmanager
    def _devices_scope(self, devices: List[Device]):
        assert all(isinstance(d, Device) for d in devices)
        self._devices = devices
        try:
            yield
        finally:
            self._devices = None

    @property
    def scheduler_context(self) -> SchedulerContext:
        if self._scheduler_context_stack:
            return self._scheduler_context_stack[-1]
        else:
            raise InvalidSchedulerAccessException("No scheduler is available in this context")


_scheduler_locals = _SchedulerLocals()


def get_scheduler_context() -> SchedulerContext:
    return _scheduler_locals.scheduler_context


def get_devices() -> List[Device]:
    return _scheduler_locals.devices


class ControllableThread(Thread, metaclass=ABCMeta):
    _monitor: threading.Condition

    def __init__(self):
        super().__init__()
        self._should_run = True

    def stop(self):
        with self._monitor:
            self._should_run = False
            self._monitor.notify_all()

    @abstractmethod
    def run(self):
        pass


class WorkerThread(ControllableThread, SchedulerContext):
    def __init__(self, scheduler, index):
        super().__init__()
        self._monitor = threading.Condition(threading.Lock())
        self.index = index
        self._scheduler = scheduler
        # Use a deque to store local tasks (a high-performance implementation would a work stealing optimized deque).
        # In this implementation the right is the "local-end", so append/pop are used by this worker and
        # appendleft/popleft are used by the scheduler or other workers.
        self._queue = deque()
        self.start()

    @property
    def scheduler(self):
        return self._scheduler

    def incr_active_tasks(self):
        self.scheduler.incr_active_tasks()

    def decr_active_tasks(self):
        self.scheduler.decr_active_tasks()

    def estimated_queue_depth(self):
        """Return the current estimated depth of this workers local queue.

        This should be considered immediately stale and may in fact be slightly wrong (+/- 1 element) w.r.t. to any
        real value of the queue depth. These limitations are to allow high-performance queue implementations that
        don't provide an atomic length operation.
        """
        # Assume this will not FAIL due to concurrent access, slightly incorrect results are not an issue.
        return len(self._queue)

    def _pop_task(self):
        """Pop a task from the queue head.
        """
        with self._monitor:
            while True:
                try:
                    if self._should_run:
                        return self._queue.pop()
                    else:
                        return None
                except IndexError:
                    self._monitor.wait()

    def steal_task_nonblocking(self):
        """Pop a task from the queue tail.

        :return: The task for None if no task was available to steal.
        """
        with self._monitor:
            try:
                return self._queue.popleft()
            except IndexError:
                return None

    def _push_task(self, task):
        """Push a local task on the queue head.
        """
        with self._monitor:
            self._queue.append(task)
            self._monitor.notify()

    def enqueue_task(self, task):
        """Push a task on the queue tail.
        """
        # For the moment, bypass the local queue and put the task in the global scheduler queue
        self.scheduler.enqueue_task(task)
        # Allowing local resource of tasks (probably only when it comes to the front of the queue) would allow threads
        # to make progress even if the global scheduler is blocked by other assignment tasks. However, it would also
        # require that the workers do some degree of resource assignment which complicates things and could break
        # correctness or efficiency guarantees. That said a local, "fast assignment" algorithm to supplement the
        # out-of-band assignment of the scheduler would probably allow Parla to efficiently run programs with
        # significantly finer-grained tasks.

        # For tasks that are already assigned it may be as simple as:
        #     self.scheduler._unassigned_resources.allocate_resources(task.assigned_device, task.assigned_amount)
        #     self._push_task(task)
        # This would need to fail over to the scheduler level enqueue if the resources is not available for assignment.

    def _enqueue_task_local(self, task):
        with self._monitor:
            self._queue.appendleft(task)
            self._monitor.notify()

    def run(self) -> None:
        try:
            with self:
                while self._should_run:
                    task: Task = self._pop_task()
                    if not task:
                        break
                    task.run()
        except Exception as e:
            logger.exception("Unexpected exception in Task handling")
            self.scheduler.stop()


class ResourcePool:
    _multiplier: float
    _monitor: Condition
    _devices: Dict[Device, Dict[str, float]]

    def __init__(self, multiplier):
        self._multiplier = multiplier
        self._monitor = threading.Condition(threading.Lock())
        self._devices = self._initial_resources(multiplier)

    @staticmethod
    def _initial_resources(multiplier):
        return {dev: {name: amt * multiplier for name, amt in dev.resources.items()} for dev in get_all_devices()}

    def allocate_resources(self, d: Device, resources: ResourceDict, *, blocking: bool = False) -> bool:
        """Allocate the resources described by `dd`.

        :param d: The device on which resources exist.
        :param resources: The resources to allocate.
        :param blocking: If True, this call will block until the resource is available and will always return True.

        :return: True iff the allocation was successful.
        """
        return self._atomically_update_resources(d, resources, -1, blocking)

    def deallocate_resources(self, d: Device, resources: ResourceDict) -> None:
        """Deallocate the resources described by `dd`.

        :param d: The device on which resources exist.
        :param resources: The resources to deallocate.
        """
        ret = self._atomically_update_resources(d, resources, 1, False)
        assert ret

    def _atomically_update_resources(self, d: Device, resources: ResourceDict, multiplier, block: bool):
        with self._monitor:
            to_release = []
            success = True
            for name, v in resources.items():
                if not self._update_resource(d, name, v * multiplier, block):
                    success = False
                    break
                else:
                    to_release.append((name, v))
            else:
                to_release.clear()

            logger.info("Attempted to allocate %s * %r (blocking %s) => %s\n%r", multiplier, (d, resources), block, success, self)
            if to_release:
                logger.info("Releasing resources due to failure: %r", to_release)

            for name, v in to_release:
                ret = self._update_resource(d, name, -v * multiplier, block)
                assert ret

            assert not success or len(to_release) == 0 # success implies to_release empty
            return success

    def _update_resource(self, dev: Device, res: str, amount: float, block: bool):
        try:
            while True: # contains return
                dres = self._devices[dev]
                if -amount <= dres[res]:
                    dres[res] += amount
                    if amount < 0:
                        self._monitor.notify_all()
                    assert dres[res] <= dev.resources[res] * self._multiplier, \
                        "{}.{} was over deallocated".format(dev, res)
                    assert dres[res] >= 0, \
                        "{}.{} was over allocated".format(dev, res)
                    return True
                else:
                    if block:
                        self._monitor.wait()
                    else:
                        return False
        except KeyError:
            raise ValueError("Resource {}.{} does not exist".format(dev, res))

    def __repr__(self):
        return "ResourcePool(devices={})".format(self._devices)


class AssignmentFailed(Exception):
    pass


class Scheduler(ControllableThread, SchedulerContext):
    def __init__(self, n_threads, period=0.01, max_worker_queue_depth=2):
        super().__init__()
        self._exceptions = []
        self._active_task_count = 1 # Start with one count that is removed when the scheduler is "exited"
        self.max_worker_queue_depth = max_worker_queue_depth
        self.period = period
        self._monitor = threading.Condition(threading.Lock())
        self._allocation_queue = deque()
        self._available_resources = ResourcePool(multiplier=1.0)
        self._unassigned_resources = ResourcePool(multiplier=max_worker_queue_depth*1.0)
        self._worker_threads = [WorkerThread(self, i) for i in range(n_threads)]
        self._should_run = True
        self.start()

    @property
    def scheduler(self):
        return self

    def __enter__(self):
        if self._active_task_count != 1:
            raise InvalidSchedulerAccessException("Schedulers can only have a single scope.")
        return super().__enter__()

    def __exit__(self, exc_type, exc_val, exc_tb):
        super().__exit__(exc_type, exc_val, exc_tb)
        self.decr_active_tasks()
        with self._monitor:
            while self._should_run:
                self._monitor.wait()
        if self._exceptions:
            # TODO: Should combine all of them into a single exception.
            raise self._exceptions[0]

    def incr_active_tasks(self):
        with self._monitor:
            self._active_task_count += 1

    def decr_active_tasks(self):
        done = False
        with self._monitor:
            self._active_task_count -= 1
            if self._active_task_count == 0:
                done = True
        if done:
            self.stop()

    def enqueue_task(self, task: Task):
        """Enqueue a task on the resource allocation queue.
        """
        with self._monitor:
            self._allocation_queue.appendleft(task)
            self._monitor.notify_all()

    def _dequeue_task(self, timeout=None) -> Optional[Task]:
        """Dequeue a task from the resource allocation queue.
        """
        with self._monitor:
            while True:
                try:
                    if self._should_run:
                        return self._allocation_queue.pop()
                    else:
                        return None
                except IndexError:
                    self._monitor.wait(timeout)
                    if timeout is not None:
                        try:
                            return self._allocation_queue.pop()
                        except IndexError:
                            return None

    def _try_assignment(self, req: DeviceSetRequirements):
        selected_devices: List[Device] = []
        for d in req.devices:
            assert len(selected_devices) < req.ndevices
            assert isinstance(d, Device)
            if self._unassigned_resources.allocate_resources(d, req.resources):
                selected_devices.append(d)
                # If we have all out devices then break.
                if len(selected_devices) == req.ndevices:
                    break
        if len(selected_devices) == req.ndevices:
            ret = DeviceSetRequirements(req.resources, req.ndevices, selected_devices)
            assert ret.exact
            return ret
        else:
            # Free any resources we already assigned
            for d in selected_devices:
                self._unassigned_resources.deallocate_resources(d, req.resources)
            return None

    def run(self) -> None:
        try:
            while self._should_run:
                task: Optional[Task] = self._dequeue_task()
                if not task:
                    # Exit if the dequeue fails. This implies a failure or shutdown.
                    break

                logger.info("Task %r: Assigning", task)
                if isinstance(task.req, OptionsRequirements):
                    for opt in task.req.possibilities:
                        a = self._try_assignment(opt)
                        if a:
                            task.assigned = True
                            task.req = a
                            break
                elif isinstance(task.req, DeviceSetRequirements) and not task.assigned:
                    a = self._try_assignment(task.req)
                    if a:
                        task.assigned = True
                        task.req = a

                if not task.req.exact:
                    task._assignment_tries = getattr(task, "_assignment_tries", 0) + 1
                    if task._assignment_tries > _ASSIGNMENT_FAILURE_WARNING_LIMIT:
                        logger.warning("Task %r: Failed to assign devices. The required resources may not be "
                                       "available on this machine at all.\n"
                                       "Available resources: %r\n"
                                       "Unallocated resources: %r",
                                       task, self._available_resources, self._unassigned_resources)
                        # Put task we cannot assign resources to at the back of the queue
                    self.enqueue_task(task)
                    # Avoid spinning when no tasks are schedulable.
                    time.sleep(self.period)
                    # TODO: There is almost certainly a better way to handle this. Add a dependency on
                    #  a task holding the needed resources?
                else:
                    # Place task in shortest worker queue if it's not too long
                    while True:  # contains break
                        worker = min(self._worker_threads, key=lambda w: w.estimated_queue_depth())
                        if worker.estimated_queue_depth() < self.max_worker_queue_depth:
                            logger.debug("Task %r: Enqueued on worker %r", task, worker)
                            worker._enqueue_task_local(task)
                            break
                        else:
                            # Delay a bit waiting for a workers queue to shorten; This is not an issue since
                            # definitionally there is plenty of work in the queues.
                            time.sleep(self.period)
        except Exception:
            logger.exception("Unexpected exception in Scheduler")
            self.stop()

    def stop(self):
        super().stop()
        for w in self._worker_threads:
            w.stop()

    def report_exception(self, e: BaseException):
        with self._monitor:
            self._exceptions.append(e)
