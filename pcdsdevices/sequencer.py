import logging

from ophyd import Device, EpicsSignal, EpicsSignalRO, Component as Cpt
from ophyd.status import DeviceStatus, SubscriptionStatus
from ophyd.utils.epics_pvs import raise_if_disconnected
from ophyd.flyers import FlyerInterface, MonitorFlyerMixin

logger = logging.getLogger(__name__)


class EventSequencer(Device, MonitorFlyerMixin, FlyerInterface):
    """
    Event Sequencer

    The LCLS Event Sequencer implemented as an Flyer; i.e it has the methods
    :meth:`.kickoff`, :meth:`.complete` and :meth:`.collect`. This allows the
    EventSequencer to be used succinctly with the `fly_during_wrapper` and
    associated preprocessor.

    Parameters
    ----------
    prefix: str
        Base prefix of the EventSequencer

    name : str
        Name of Event Sequencer object

    Examples
    --------
    Run the EventSequencer throughout my scan

    .. code::

        fly_during_wrapper(scan([det], motor, ...), [sequencer])

    Run the EventSequencer at each step in my scan after completing the
    motor move and detector reading:

    .. code::

        scan([sequencer], motor, ....)

    Note
    ----
    It is ambiguous what the correct behavior for the EventSequencer is when we
    pause and resume during a scan. The current implementation will stop the
    EventSequencer and restart the sequence from the beginning. This may impact
    applications which depend on a long single looped sequence running through
    out the scan
    """
    play_control = Cpt(EpicsSignal, ':PLYCTL', kind='omitted')
    sequence_length = Cpt(EpicsSignal, ':LEN', kind='config')
    current_step = Cpt(EpicsSignal, ':CURSTP', kind='normal')
    play_count = Cpt(EpicsSignal, ':PLYCNT', kind='normal')
    total_play_count = Cpt(EpicsSignalRO, ':TPLCNT', kind='normal')
    play_status = Cpt(EpicsSignalRO, ':PLSTAT', auto_monitor=True,
                      kind='normal')
    play_mode = Cpt(EpicsSignal, ':PLYMOD', kind='config')
    sync_marker = Cpt(EpicsSignal, ':SYNCMARKER', kind='config')
    next_sync = Cpt(EpicsSignal, ':SYNCNEXTTICK', kind='config')
    pulse_req = Cpt(EpicsSignal, ':BEAMPULSEREQ', kind='config')
    rep_count = Cpt(EpicsSignal, ":REPCNT", kind='config')
    sequence_owner = Cpt(EpicsSignalRO, ':HUTCH_NAME', kind='omitted')

    def __init__(self, prefix, *, name=None, monitor_attrs=None, **kwargs):
        monitor_attrs = monitor_attrs or ['current_step', 'play_count']
        # Device initialization
        super().__init__(prefix, name=name,
                         monitor_attrs=monitor_attrs, **kwargs)

    @raise_if_disconnected
    def kickoff(self):
        """
        Start the EventSequencer

        Returns
        -------
        status : SubscriptionStatus
            Status indicating whether or not the EventSequencer has started
        """
        self.start()
        # Start monitor signals
        super().kickoff()

        # Create our status
        def done(*args, value=None, old_value=None, **kwargs):
            return value == 2 and old_value == 0

        # Create our status object
        return SubscriptionStatus(self.play_status, done, run=True)

    @raise_if_disconnected
    def start(self):
        """
        Start the EventSequencer
        """
        # Start the sequencer
        logger.debug("Starting EventSequencer ...")
        self.play_control.put(1)

    def trigger(self):
        """
        Trigger the EventSequencer

        This method reconfigures the EventSequencer to take a new reading. This
        means:

            * Stopping the EventSequencer if it is already running
            * Restarting the EventSequencer

        The returned status object will indicate different behavior based on
        the configuration of the EventSequencer itself. If set to "Run
        Forever", the status object merely indicates that we have succesfully
        started our sequence. Otherwise, the status object will be completed
        when the sequence we have set it to play is complete.
        """
        # Stop the Sequencer if it is already running
        self.stop()
        # Fire the EventSequencer
        self.start()
        # If we are running forever, count this is as triggered
        if self.play_mode.get() == 2:
            logger.debug("EventSequencer is set to run forever, "
                         "trigger is complete")
            return DeviceStatus(self, done=True, success=True)

        # Create our status
        def done(*args, value=None, old_value=None, **kwargs):
            return value == 2 and old_value == 0

        # Create our status object
        return SubscriptionStatus(self.play_status, done, run=True)

    def pause(self):
        """Stop the event sequencer and stop monitoring events"""
        # Order a stop
        self.stop()
        # Pause monitoring
        super().pause()

    def resume(self):
        """Resume the EventSequencer procedure"""
        super().resume()
        self.start()

    def complete(self):
        """
        Complete the EventSequencer's current sequence

        The result of this method varies on the mode that the EventSequencer is
        configured. If the EventSequencer is either set to "Run Once" or "Run N
        Times" this method allows the current sequence to complete and returns
        a status object that indicates a successful completion. However, this
        mode of operation does not make sense if the EventSequencer is in
        "Run Forever" mode. In this case, the EventSequencer is stopped
        immediately and a completed status object is returned.

        Returns
        -------
        status: DeviceStatus or SubscriptionStatus
            Status indicating completion of sequence

        Note
        ----
        If you want to stop the sequence from running regardless of
        configuration use the :meth:`.stop` command.

        """
        # Clear the monitor subscriptions
        super().complete()
        # If we are running forever we can stop whenever
        if self.play_mode.get() == 2:
            logger.debug("EventSequencer is set to run forever, "
                         "stopping immediately")
            self.stop()
            return DeviceStatus(self, done=True, success=True)

        # Otherwise we should wait for the sequencer to end
        def done(*args, value=None, old_value=None, **kwargs):
            return value == 0 and old_value == 2

        # Create a SubscriptionStatus
        logger.debug("EventSequencer has a determined stopping point, "
                     " waiting for sequence to complete")
        st = SubscriptionStatus(self.play_status, done, run=True)
        return st

    def stop(self):
        """Stop the EventSequencer"""
        logger.debug("Stopping the EventSequencer")
        self.play_control.put(0)
