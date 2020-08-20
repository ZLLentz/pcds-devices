import logging
import typing

import numpy as np
from ophyd.device import Component as Cpt
from ophyd.device import FormattedComponent as FCpt
from ophyd.pseudopos import PseudoPositioner as _PseudoPositioner
from ophyd.pseudopos import (PseudoSingle, pseudo_position_argument,
                             real_position_argument)
from scipy.constants import speed_of_light

from .interface import FltMvInterface
from .signal import NotepadLinkedSignal
from .sim import FastMotor
from .utils import convert_unit

logger = logging.getLogger(__name__)


class PseudoSingleInterface(PseudoSingle, FltMvInterface):
    """PseudoSingle with FltMvInterface mixed in."""
    notepad_setpoint = Cpt(
        NotepadLinkedSignal, ':OphydSetpoint',
        notepad_metadata={'record': 'ao', 'default_value': 0.0},
    )

    notepad_readback = Cpt(
        NotepadLinkedSignal, ':OphydReadback',
        notepad_metadata={'record': 'ai', 'default_value': 0.0},
    )

    def __init__(self, prefix='', parent=None, **kwargs):
        if not prefix:
            # PseudoSingle generally does not get a prefix. Fix that here,
            # or 'notepad_setpoint' and 'notepad_readback' will have no
            # prefix.
            attr_name = kwargs['attr_name']
            prefix = f'{parent.prefix}:{attr_name}'

        super().__init__(prefix=prefix, parent=parent, **kwargs)


class PseudoPositioner(_PseudoPositioner):
    """
    This is a PCDS-specific PseudoPositioner subclass which adds support
    for NotepadLinkedSignal.  The functionality of the class is otherwise
    identical to ophyd's PseudoPositioner.

    """ + _PseudoPositioner.__doc__

    def _update_notepad_ioc(self, position, attr):
        """
        Update the notepad IOC with a fully-specified ``PseudoPos``.

        Parameters
        ----------
        position : PseudoPos
            The position.

        attr : str
            The signal attribute name, such as ``notepad_setpoint``.
        """
        for positioner, value in zip(self._pseudo, position):
            try:
                signal = getattr(positioner, attr, None)
                if signal is None:
                    continue
                if signal.connected and signal.write_access:
                    if signal.get(use_monitor=True) != value:
                        signal.put(value, wait=False)
            except Exception as ex:
                self.log.debug('Failed to update notepad %s to position %s',
                               attr, value, exc_info=ex)

    @pseudo_position_argument
    def move(self, position, wait=True, timeout=None, moved_cb=None):
        '''
        Move to a specified position, optionally waiting for motion to
        complete.

        Parameters
        ----------
        position
            Pseudo position to move to.

        moved_cb : callable
            Call this callback when movement has finished. This callback must
            accept one keyword argument: 'obj' which will be set to this
            positioner instance.

        timeout : float, optional
            Maximum time to wait for the motion. If None, the default timeout
            for this positioner is used.

        Returns
        -------
        status : MoveStatus

        Raises
        ------
        TimeoutError
            When motion takes longer than `timeout`.

        ValueError
            On invalid positions.

        RuntimeError
            If motion fails other than timing out.
        '''
        status = super().move(position, wait=wait, timeout=timeout,
                              moved_cb=moved_cb)
        self._update_notepad_ioc(position, 'notepad_setpoint')
        return status

    def _update_position(self):
        """Update the pseudo position based on that of the real positioners."""
        position = super()._update_position()
        self._update_notepad_ioc(position, 'notepad_readback')
        return position


class SyncAxesBase(PseudoPositioner, FltMvInterface):
    """
    Synchronized Axes.

    This will move all axes in a coordinated way, retaining offsets.

    This can be configured to report its position as the min, max, mean, or any
    custom function acting on a list of positions. Min is the default.

    You should subclass this by adding real motors as components. The class
    will pick them up and include them correctly into the coordinated move.

    An example:

    .. code-block:: python

       class Parallel(SyncAxesBase):
           left = Cpt(EpicsMotor, ':01')
           right = Cpt(EpicsMotor, ':02')

    Like all `~ophyd.pseudopos.PseudoPositioner` classes, any subclass of
    `~ophyd.positioner.PositionerBase` will be included in the synchronized
    move.
    """

    pseudo = Cpt(PseudoSingleInterface)

    def __init__(self, *args, **kwargs):
        if self.__class__ is SyncAxesBase:
            raise TypeError(('SyncAxesBase must be subclassed with '
                             'the axes to synchronize included as '
                             'components'))
        super().__init__(*args, **kwargs)
        self._offsets = {}

    def calc_combined(self, real_position):
        """
        Calculate the combined pseudo position.

        By default, this is just the position of our first axis.

        Parameters
        ----------
        real_position : ~typing.NamedTuple
            The positions of each of the real motors, accessible by name.

        Returns
        -------
        pseudo_position : float
            The combined position of the axes.
        """

        return real_position[0]

    def save_offsets(self):
        """
        Save the current offsets for the synchronized assembly.

        If not done earlier, this will be automatically run before it is first
        needed (generally, right before the first move).
        """

        pos = self.real_position
        combo = self.calc_combined(pos)
        offsets = {fld: getattr(pos, fld) - combo for fld in pos._fields}
        self._offsets = offsets
        logger.debug('Offsets %s cached', offsets)

    @pseudo_position_argument
    def forward(self, pseudo_pos):
        """Composite axes move to the combined axis position plus an offset."""
        if not self._offsets:
            self.save_offsets()
        real_pos = {}
        for axis, offset in self._offsets.items():
            real_pos[axis] = pseudo_pos.pseudo + offset
        return self.RealPosition(**real_pos)

    @real_position_argument
    def inverse(self, real_pos):
        """Combined axis readback is the mean of the composite axes."""
        return self.PseudoPosition(pseudo=self.calc_combined(real_pos))


class DelayBase(PseudoPositioner, FltMvInterface):
    """
    Laser delay stage to rescale a physical axis to a time axis.

    The optical laser travels along the motor's axis and bounces off a number
    of mirrors, then continues to the destination. In this way, the path length
    of the laser changes, which introduces a variable delay. This delay is a
    simple multiplier based on the speed of light.

    Attributes
    ----------
    delay : ~ophyd.pseudopos.PseudoSingle
        The fake axis. It has configurable units and number of bounces.

    motor : ~ophyd.positioner.PositionerBase
        The real axis. This can be a number of things based on the inheriting
        class, but it must have a valid ``egu`` so we know how to convert to
        the time axis.

    Parameters
    ----------
    prefix : str
        The EPICS prefix of the real motor.

    name : str
        A name to assign to this delay stage.

    egu : str, optional
        The units to use for the delay axis. The default is seconds. Any
        time unit is acceptable.

    n_bounces : int, optional
        The number of times the laser bounces on the delay stage, e.g. the
        number of mirrors that this stage moves. The default is 2, a delay
        branch that bounces the laser back along the axis it enters.
    """

    delay = FCpt(PseudoSingleInterface, egu='{self.egu}', add_prefix=['egu'])
    motor = None

    def __init__(self, *args, egu='s', n_bounces=2, **kwargs):
        if self.__class__ is DelayBase:
            raise TypeError(('DelayBase must be subclassed with '
                             'a "motor" component, the real motor to move.'))
        self.n_bounces = n_bounces
        super().__init__(*args, egu=egu, **kwargs)

    @pseudo_position_argument
    def forward(self, pseudo_pos):
        """Convert delay unit to motor unit."""
        seconds = convert_unit(pseudo_pos.delay, self.delay.egu, 'seconds')
        meters = seconds * speed_of_light / self.n_bounces
        motor_value = convert_unit(meters, 'meters', self.motor.egu)
        return self.RealPosition(motor=motor_value)

    @real_position_argument
    def inverse(self, real_pos):
        """Convert motor unit to delay unit."""
        meters = convert_unit(real_pos.motor, self.motor.egu, 'meters')
        seconds = meters / speed_of_light * self.n_bounces
        delay_value = convert_unit(seconds, 'seconds', self.delay.egu)
        return self.PseudoPosition(delay=delay_value)


class SimDelayStage(DelayBase):
    motor = Cpt(FastMotor, init_pos=0, egu='mm')


class PseudoSingleInterface(PseudoSingle, FltMvInterface):
    """PseudoSingle with FltMvInterface mixed in."""
    pass


class LookupTablePositioner(PseudoPositioner):
    """
    A pseudo positioner which uses a look-up table to compute positions.

    Currently supports 1 pseudo positioner and 1 "real" positioner, which
    should be columns of a 2D numpy.ndarray ``table``.

    Parameters
    ----------
    prefix : str
        The EPICS prefix of the real motor.

    name : str
        A name to assign to this delay stage.

    table : np.ndarray
        The table of information.

    column_names : list of str
        List of column names, corresponding to the component attribute names.
        That is, if you have a real motor ``mtr = Cpt(EpicsMotor, ...)``,
        ``"mtr"`` should be in the list of column names of the table.
    """

    table: np.ndarray
    column_names: typing.Tuple[str, ...]
    _table_data_by_name: typing.Dict[str, np.ndarray]

    def __init__(self, *args,
                 table: np.ndarray,
                 column_names: typing.List[str],
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.table = table
        self.column_names = tuple(column_names)
        missing = set()
        for positioner in self._real + self._pseudo:
            if positioner.attr_name not in column_names:
                missing.add(positioner.attr_name)

        if missing:
            raise ValueError(f'Positioners {missing} not present in the table')

        if len(column_names) != self.table.shape[-1]:
            raise ValueError(
                'Incorrect number of column names for the given table.'
            )

        # For now, no fancy interpolation options
        if len(table.shape) != 2:
            raise ValueError(f'Unsupported table dimensions: {table.shape}')

        self._table_data_by_name = {
            column_name: self.table[:, idx]
            for idx, column_name in enumerate(column_names)
        }

        for attr, data in self._table_data_by_name.items():
            obj = getattr(self, attr)
            limits = (np.min(data), np.max(data))
            if isinstance(obj, PseudoSingle):
                obj._limits = limits
            elif hasattr(obj, 'limits'):
                try:
                    obj.limits = limits
                except AttributeError:
                    self.log.debug('Unable to set limits for %s', obj.name)

    @pseudo_position_argument
    def forward(self, pseudo_pos: tuple) -> tuple:
        '''
        Calculate a RealPosition from a given PseudoPosition

        Must be defined on the subclass.

        Parameters
        ----------
        pseudo_pos : PseudoPosition
            The pseudo position input, a namedtuple.

        Returns
        -------
        real_position : RealPosition
            The real position output, a namedtuple.
        '''
        values = pseudo_pos._asdict()

        pseudo_field, = self.PseudoPosition._fields
        real_field, = self.RealPosition._fields

        real_value = np.interp(
            values[pseudo_field],
            self._table_data_by_name[pseudo_field],
            self._table_data_by_name[real_field]
        )
        return self.RealPosition(**{real_field: real_value})

    @real_position_argument
    def inverse(self, real_pos: tuple) -> tuple:
        '''Calculate a PseudoPosition from a given RealPosition

        Must be defined on the subclass.

        Parameters
        ----------
        real_position : RealPosition
            The real position input

        Returns
        -------
        pseudo_pos : PseudoPosition
            The pseudo position output
        '''
        values = real_pos._asdict()
        pseudo_field, = self.PseudoPosition._fields
        real_field, = self.RealPosition._fields

        pseudo_value = np.interp(
            values[real_field],
            self._table_data_by_name[real_field],
            self._table_data_by_name[pseudo_field]
        )
        return self.PseudoPosition(**{pseudo_field: pseudo_value})
