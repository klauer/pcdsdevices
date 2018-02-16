import os
import functools
import threading
import copy
import enum
import logging

from ophyd.status import Status, wait as status_wait
from ophyd.flyers import FlyerInterface
from bluesky.plan_stubs import kickoff, complete
from bluesky.preprocessors import fly_during_wrapper
from bluesky.utils import make_decorator

logger = logging.getLogger(__name__)

try:
    import pydaq
except ImportError:
    logger.warning('pydaq not in environment. Will not be able to use DAQ!')

# Wait up to this many seconds for daq to be ready for a begin call
BEGIN_TIMEOUT = 2


# Wrapper to make sure we're connected
def check_connect(f):
    @functools.wraps(f)
    def wrapper(self, *args, **kwargs):
        logger.debug('Checking for daq connection')
        if not self.connected:
            msg = 'DAQ is not connected. Attempting to connect...'
            logger.info(msg)
            self.connect()
        if self.connected:
            logger.debug('Daq is connected')
            return f(self, *args, **kwargs)
        else:
            err = 'Could not connect to DAQ'
            logger.error(err)
            raise RuntimeError(err)
    return wrapper


class Daq(FlyerInterface):
    """
    The LCLS1 DAQ as a flyer object. This uses the pydaq module to connect with
    a running daq instance, controlling it via socket commands. It can be used
    as a flyer in a bluesky plan to have the daq start at the beginning of the
    run and end at the end of the run. It has additional knobs for pausing
    and resuming acquisition. This can be done using three modes:

    on:      Always take events during the run
    manual:  Take events when `yield from calibcycle()` is used
    auto:    Take events between `create` and `save` messages

    Unlike a normal bluesky flyer, this has no data to report to the RunEngine
    on the collect call. No data will pass into the python layer from the daq.
    """
    _state_enum = enum.Enum('PydaqState',
                            'Disconnected Connected Configured Open Running',
                            start=0)
    _mode_enum = enum.Enum('ScanMode', 'on manual auto', start=0)
    default_config = dict(events=None,
                          duration=None,
                          use_l3t=False,
                          record=False,
                          controls=None,
                          mode=_mode_enum.on)

    def __init__(self, name=None, platform=0, parent=None, RE=None):
        super().__init__()
        self.name = name or 'daq'
        self.parent = parent
        self._control = None
        self._config = None
        self._host = os.uname()[1]
        self._plat = platform
        self._is_bluesky = False
        self._RE = RE
        register_daq(self)

    # Convenience properties
    @property
    def connected(self):
        return self._control is not None

    @property
    def configured(self):
        return self._config is not None

    @property
    def state(self):
        if self.connected:
            logger.debug('calling Daq.control.state()')
            num = self._control.state()
            return self._state_enum(num).name
        else:
            return 'Disconnected'

    # Interactive methods
    def connect(self):
        """
        Connect to the DAQ instance, giving full control to the Python process.
        """
        logger.debug('Daq.connect()')
        if self._control is None:
            try:
                logger.debug('instantiate Daq.control = pydaq.Control(%s, %s)',
                             self._host, self._plat)
                self._control = pydaq.Control(self._host, platform=self._plat)
                logger.debug('Daq.control.connect()')
                self._control.connect()
                msg = 'Connected to DAQ'
            except Exception:
                logger.debug('del Daq.control')
                del self._control
                self._control = None
                msg = ('Failed to connect to DAQ - check that it is up and '
                       'allocated.')
        else:
            msg = 'Connect requested, but already connected to DAQ'
        logger.info(msg)

    def disconnect(self):
        """
        Disconnect from the DAQ instance, giving control back to the GUI
        """
        logger.debug('Daq.disconnect()')
        if self._control is not None:
            self._control.disconnect()
        del self._control
        self._control = None
        self._config = None
        logger.info('DAQ is disconnected.')

    @check_connect
    def wait(self, timeout=None):
        """
        Pause the thread until the DAQ is done aquiring.

        Parameters
        ----------
        timeout: float
            Maximum time to wait in seconds.
        """
        logger.debug('Daq.wait()')
        if self.state == 'Running':
            status = self._get_end_status()
            status_wait(status, timeout=timeout)

    def begin(self, events=None, duration=None, use_l3t=None, controls=None,
              wait=False):
        """
        Start the daq with the current configuration. Block until
        the daq has begun acquiring data. Optionally block until the daq has
        finished aquiring data.

        Parameters
        ----------
        events: int, optional
            Number events to stop acquisition at.

        duration: int, optional
            Time to run the daq in seconds.

        wait: bool, optional
            If switched to True, wait for the daq to finish aquiring data.
        """
        logger.debug('Daq.begin(events=%s, duration=%s, wait=%s)',
                     events, duration, wait)
        begin_status = self.kickoff(events=events, duration=duration,
                                    use_l3t=use_l3t, controls=controls)
        status_wait(begin_status, timeout=BEGIN_TIMEOUT)
        if wait:
            self.wait()

    @check_connect
    def stop(self):
        """
        Stop the current acquisition, ending it early.
        """
        logger.debug('Daq.stop()')
        self._control.stop()

    @check_connect
    def end_run(self):
        """
        Stop the daq if it's running, then mark the run as finished.
        """
        logger.debug('Daq.end_run()')
        self.stop()
        self._control.endrun()

    # Flyer interface
    @check_connect
    def kickoff(self, events=None, duration=None, use_l3t=None, controls=None):
        """
        Begin acquisition. This method is non-blocking.

        Returns
        -------
        ready_status: DaqStatus
            Status that will be marked as 'done' when the daq has begun to
            record data.
        """
        logger.debug('Daq.kickoff()')

        self._check_duration(duration)
        if not self.configured:
            self.configure()

        def start_thread(control, status, events, duration, use_l3t, controls):
            begin_args = self._begin_args(events, duration, use_l3t, controls)
            logger.debug('daq.control.begin(%s)', begin_args)
            tmo = BEGIN_TIMEOUT
            dt = 0.1
            # It can take up to 0.4s after a previous begin to be ready
            while tmo > 0:
                if self.state in ('Configured', 'Open'):
                    break
                else:
                    tmo -= dt
            if self.state in ('Configured', 'Open'):
                control.begin(**begin_args)
                logger.debug('Marking kickoff as complete')
                status._finished(success=True)
            else:
                logger.debug('Marking kickoff as failed')
                status._finished(success=False)

        begin_status = Status(obj=self)
        watcher = threading.Thread(target=start_thread,
                                   args=(self._control, begin_status, events,
                                         duration, use_l3t, controls))
        watcher.start()
        return begin_status

    def complete(self):
        """
        If the daq is freely running, this will stop the daq.
        Otherwise, we'll let the daq finish up the fixed-length acquisition.

        Return a status object that will be marked as 'done' when the DAQ has
        finished acquiring.

        Returns
        -------
        end_status: DaqStatus
        """
        logger.debug('Daq.complete()')
        end_status = self._get_end_status()
        config = self.read_configuration()
        if not any(config['events'], config['duration']):
            # Configured to run forever
            self.stop()
        return end_status

    def _get_end_status(self):
        """
        Return a status object that will be marked as 'done' when the DAQ has
        finished acquiring.

        Returns
        -------
        end_status: DaqStatus
        """
        logger.debug('Daq._get_end_status()')

        def finish_thread(control, status):
            try:
                logger.debug('Daq.control.end()')
                control.end()
            except RuntimeError:
                pass  # This means we aren't running, so no need to wait
            status._finished(success=True)
            logger.debug('Marked acquisition as complete')
        end_status = Status(obj=self)
        watcher = threading.Thread(target=finish_thread,
                                   args=(self._control, end_status))
        watcher.start()
        return end_status

    def collect(self):
        """
        End the run.

        As per the bluesky interface, this is a generator that is expected to
        output partial event documents. However, since we don't have any events
        to report to python, this will be a generator that immediately ends.
        """
        logger.debug('Daq.collect()')
        self.end_run()
        return
        yield

    def describe_collect(self):
        """
        As per the bluesky interface, this is how you interpret the null data
        from collect. There isn't anything here, as nothing will be collected.
        """
        logger.debug('Daq.describe_configuration()')
        return {}

    @check_connect
    def configure(self, events=None, duration=None, record=False,
                  use_l3t=False, controls=None, mode=None):
        """
        Changes the daq's configuration for the next run.

        Parameters
        ----------
        events: int, optional
            If provided, the daq will run for this many events before
            stopping, unless we override in begin.
            If not provided, we'll use the duration argument instead.

        duration: int, optional
            If provided, the daq will run for this many seconds before
            stopping, unless we override in begin.
            If not provided, and events was also not provided, an empty call to
            begin() will run indefinitely.

        use_l3t: bool, optional
            If True, an events argument to begin will be reinterpreted to only
            count events that pass the level 3 trigger.

        record: bool, optional
            If True, we'll record the data. Otherwise, we'll run without
            recording.

        controls: dict{str: device}, optional
            If provided, values from these will make it into the DAQ data
            stream as variables. We will check device.position and device.value
            for quantities to use and we will update these values each time
            begin is called.

        mode: str or int, optional
            This determines our run control during a Bluesky scan with a
            RunEngine attached to a Daq object. There are three modes, with the
            `on` mode as the default:

            `on`     (0): Start taking events at open_run, stop at close_run
            `manual` (1): Only take events after a call to trigger
            `auto`   (2): Start taking events at create, stop at save

        Returns
        -------
        old, new: tuple of dict
        """
        logger.debug(('Daq.configure(events=%s, duration=%s, record=%s, '
                      'use_l3t=%s, controls=%s, mode=%s)'),
                     events, duration, record, use_l3t, controls, mode)
        state = self.state
        if state not in ('Connected', 'Configured'):
            raise RuntimeError('Cannot configure from state {}!'.format(state))

        self._check_duration(duration)

        old = self.read_configuration()

        if mode is None:
            mode = old['mode']
        try:
            mode = getattr(self._mode_enum, mode)
        except AttributeError:
            try:
                mode = self._mode_enum(mode)
            except ValueError:
                raise ValueError('{} is not a valid scan mode!'.format(mode))

        config_args = self._config_args(record, use_l3t, controls)
        try:
            logger.debug('Daq.control.configure(%s)',
                         config_args)
            self._control.configure(**config_args)
            # self._config should reflect exactly the arguments to configure,
            # this is different than the arguments that pydaq.Control expects
            self._config = dict(events=events, duration=duration,
                                record=record, use_l3t=use_l3t,
                                controls=controls, mode=mode)
            msg = 'Daq configured'
            logger.info(msg)
        except Exception:
            self._config = None
            msg = 'Failed to configure!'
            logger.exception(msg)
        new = self.read_configuration()
        return old, new

    def _config_args(self, record, use_l3t, controls):
        """
        For a given set of arguments to configure, return the arguments that
        should be sent to control.configure.

        Returns
        -------
        config_args: dict
        """
        logger.debug('Daq._config_args(%s, %s, %s)',
                     record, use_l3t, controls)
        config_args = {}
        config = self.read_configuration()
        if record is None:
            config_args['record'] = config['record']
        else:
            config_args['record'] = record
        if use_l3t:
            config_args['l3t_events'] = 0
        else:
            config_args['events'] = 0
        if controls is not None:
            config_args['controls'] = self._ctrl_arg(controls)
        for key, value in list(config_args.items()):
            if value is None:
                del config_args[key]
        return config_args

    def _ctrl_arg(self, ctrl_dict):
        """
        Assemble the list of (str, val) pairs from a {str: device} dictionary.
        """
        ctrl_arg = []
        for key, device in ctrl_dict.items():
            try:
                val = device.position
            except AttributeError:
                val = device.value
            ctrl_arg.append((key, val))
        return ctrl_arg

    def _begin_args(self, events, duration, use_l3t, controls):
        """
        For a given set of arguments to begin, return the arguments that should
        be sent to control.begin

        Returns
        -------
        begin_args: dict
        """
        logger.debug('Daq._begin_args(%s, %s, %s, %s)',
                     events, duration, use_l3t, controls)
        begin_args = {}
        config = self.read_configuration()
        if all((self._is_bluesky, not config['mode'] == self._mode_enum.on,
                not self.state == 'Open')):
            # Open a run without taking events
            events = 1
            duration = None
            use_l3t = False
        if events is None and duration is None:
            events = config['events']
            duration = config['duration']
        if events is not None:
            if use_l3t is None and self.configured:
                use_l3t = config['use_l3t']
            if use_l3t:
                begin_args['l3t_events'] = events
            else:
                begin_args['events'] = events
        elif duration is not None:
            secs = int(duration)
            nsec = int((duration - secs) * 1e9)
            begin_args['duration'] = [secs, nsec]
        else:
            begin_args['events'] = 0  # Run until manual stop
        if controls is None:
            ctrl_dict = config['controls']
            if ctrl_dict is not None:
                begin_args['controls'] = self._ctrl_arg(ctrl_dict)
        else:
            begin_args['controls'] = self._ctrl_arg(controls)
        return begin_args

    def _check_duration(self, duration):
        if duration is not None and duration < 1:
            msg = ('Duration argument less than 1 is unreliable. Please '
                   'use the events argument to specify the length of '
                   'very short runs.')
            raise RuntimeError(msg)

    def read_configuration(self):
        """
        Returns
        -------
        config: dict
            Mapping of config key to current configured value.
        """
        logger.debug('Daq.read_configuration()')
        if self._config is None:
            config = self.default_config
        else:
            config = self._config
        return copy.copy(config)

    def describe_configuration(self):
        """
        Returns
        -------
        config_desc: dict
            Mapping of config key to field metadata.
        """
        logger.debug('Daq.describe_configuration()')
        try:
            config = self.read_configuration()
            controls_shape = [len(config['control']), 2]
        except (RuntimeError, AttributeError):
            controls_shape = None
        return dict(events=dict(source='daq_events_in_run',
                                dtype='number',
                                shape=None),
                    duration=dict(source='daq_run_duration',
                                  dtype='number',
                                  shape=None),
                    use_l3t=dict(source='daq_use_l3trigger',
                                 dtype='number',
                                 shape=None),
                    record=dict(source='daq_record_run',
                                dtype='number',
                                shape=None),
                    controls=dict(source='daq_control_vars',
                                  dtype='array',
                                  shape=controls_shape),
                    always_on=dict(source='daq_always_on',
                                   dtype='number',
                                   shape=None))

    def stage(self):
        """
        Nothing to be done here, but we overwrite the default stage because it
        is expecting sub devices.

        Returns
        -------
        staged: list
            list of devices staged
        """
        logger.debug('Daq.stage()')
        return [self]

    def unstage(self):
        """
        Nothing to be done here, but we overwrite the default unstage because
        it is expecting sub devices.

        Returns
        -------
        unstaged: list
            list of devices unstaged
        """
        logger.debug('Daq.unstage()')
        return [self]

    def pause(self):
        """
        Stop acquiring data, but don't end the run.
        """
        logger.debug('Daq.pause()')
        if self.state == 'Running':
            self.stop()

    def resume(self):
        """
        Continue acquiring data in a previously paused run.
        """
        logger.debug('Daq.resume()')
        if self.state == 'Open':
            self.begin()

    def _interpret_message(self, msg):
        """
        msg_hook for the daq to decide when to run and when not to run,
        provided we've been configured for always_on=False.
        Also looks for 'open_run' and 'close_run' docs to keep track of when
        we're in a Bluesky plan and when we're not.
        """
        logger.debug('Daq._interpret_message(%s)', msg)
        cmds = ('open_run', 'close_run', 'create', 'save')
        if msg.command not in cmds:
            return
        config = self.read_configuration()
        if msg.command == 'open_run':
            self._is_bluesky = True
        elif msg.command == 'close_run':
            self._is_bluesky = False
        if config['mode'] == self._mode_enum.on:
            if msg.command == 'create':
                # If already runing, pause first to start a fresh begin
                self.pause()
                self.resume()
            elif msg.command == 'save':
                if any(config['events'], config['duration']):
                    self.wait()
                else:
                    self.pause()

    def __del__(self):
        if self.state in ('Open', 'Running'):
            self.end_run()
        self.disconnect()


_daq_instance


def register_daq(daq):
    """
    Called by Daq at the end of __init__ to save our one daq instance as the
    True Daq. There will always only be one Daq.
    """
    global _daq_instance
    _daq_instance = daq


def daq_wrapper(plan):
    """
    Run the plan with the daq. This must be applied outside the run_wrapper.
    All configuration must be done before entering the daq_wrapper.
    """
    try:
        daq = _daq_instance
        daq._RE.msg_hook = daq._interpret_message
        yield from functools.partial(fly_during_wrapper, flyers=[daq])
        daq._RE.msg_hook = None
    except Exception:
        daq._RE.msg_hook = None
        raise


daq_decorator = make_decorator(daq_wrapper)


def calib_cycle():
    """
    Plan to put the daq through a single calib cycle. This will start the daq
    with the configured parameters and wait until completion. This will raise
    an exception if the daq is configured to run forever or if we aren't using
    the daq_wrapper.
    """
    daq = _daq_instance
    if not daq._is_bluesky:
        raise RuntimeError('Daq is not attached to the RunEngine! We need to '
                           'use a daq_wrapper on our plan to run with the '
                           'daq!')
    config = daq.read_configuration()
    if not any(config['events'], config['duration']):
        raise RuntimeError('Daq is configured to run forever, cannot calib '
                           'cycle. Please call daq.configure with a nonzero '
                           'events or duration argument.')
    yield from kickoff(daq, wait=True)
    yield from complete(daq, wait=True)
