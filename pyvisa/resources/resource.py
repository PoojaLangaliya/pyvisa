# -*- coding: utf-8 -*-
"""
    pyvisa.resources.resource
    ~~~~~~~~~~~~~~~~~~~~~~~~~

    High level wrapper for a Resource.

    This file is part of PyVISA.

    :copyright: 2014 by PyVISA Authors, see AUTHORS for more details.
    :license: MIT, see LICENSE for more details.
"""
import contextlib
import copy
import math
import time
from typing import Any, Callable, ContextManager, Iterator, Optional, Type, Union, cast

from typing_extensions import Literal

from .. import attributes, constants, errors, highlevel, logger, rname
from ..typing import VISASession


class WaitResponse(object):
    """Class used in return of wait_on_event. It properly closes the context upon delete.
       A call with event_type of 0 (normally used when timed_out is True) will be
       recorded as None, otherwise it records the proper EventType enum.

    """

    def __init__(self, event_type, context, ret, visalib, timed_out=False):
        if event_type == 0:
            self.event_type = None
        else:
            self.event_type = constants.EventType(event_type)
        self.context = context
        self.ret = ret
        self._visalib = visalib
        self.timed_out = timed_out

    def __del__(self) -> None:
        if self.context != None:
            try:
                self._visalib.close(self.context)
            except errors.VisaIOError:
                pass


class Resource(object):
    """Base class for resources.

    Do not instantiate directly, use
    :meth:`pyvisa.highlevel.ResourceManager.open_resource`.

    """

    #: Reference to the resource manager used by this resource
    resource_manager: highlevel.ResourceManager

    #: VISA name of the resource  "GPIB::10::INSTR"
    resource_name: str

    #: Reference to the VISA library instance used by the resource
    visalib: highlevel.VisaLibraryBase

    # XXX manually set the attributes and make this class only check it was done correctly
    # Will improve readability and typing both !
    @classmethod
    def register(
        cls, interface_type: constants.InterfaceType, resource_class: str
    ) -> Callable[[Type["Resource"]], Type["Resource"]]:
        def _internal(python_class):
            highlevel.ResourceManager.register_resource_class(
                interface_type, resource_class, python_class
            )

            # If the class already has this attribute,
            # it means that a parent class was registered first.
            # We need to copy the current list and extended it.
            attrs = copy.copy(getattr(python_class, "visa_attributes_classes", []))

            for attr in attributes.AttributesPerResource[
                (interface_type, resource_class)
            ]:
                attrs.append(attr)
                if not hasattr(python_class, attr.py_name) and attr.py_name != "":
                    setattr(python_class, attr.py_name, attr())
            for attr in attributes.AttributesPerResource[attributes.AllSessionTypes]:
                attrs.append(attr)
                if not hasattr(python_class, attr.py_name) and attr.py_name != "":
                    setattr(python_class, attr.py_name, attr())

            setattr(python_class, "visa_attributes_classes", attrs)
            return python_class

        return _internal

    def __init__(
        self, resource_manager: highlevel.ResourceManager, resource_name: str
    ) -> None:
        self._resource_manager = resource_manager
        self.visalib = self._resource_manager.visalib

        # We store the resource name and use preferably the private attr over
        # the public descriptor internally because the public descriptor
        # requires a live instance the VISA library, which means it is much
        # slower but also can cause issue in error reporting when accessing the
        # repr
        self._resource_name: str
        try:
            # Attempt to normalize the resource name. Can fail for aliases
            self._resource_name = str(rname.ResourceName.from_string(resource_name))
        except rname.InvalidResourceName:
            self._resource_name = resource_name

        self._logging_extra = {
            "library_path": self.visalib.library_path,
            "resource_manager.session": self._resource_manager.session,
            "resource_name": self._resource_name,
            "session": None,
        }

        #: Session handle.
        self._session: Optional[VISASession] = None

    @property
    def session(self) -> VISASession:
        """Resource session handle.

        :raises: :class:`pyvisa.errors.InvalidSession` if session is closed.
        """
        if self._session is None:
            raise errors.InvalidSession()
        return self._session

    @session.setter
    def session(self, value: Optional[VISASession]) -> None:
        self._session = value

    def __del__(self) -> None:
        if self._session is not None:
            self.close()

    def __str__(self) -> str:
        return "%s at %s" % (self.__class__.__name__, self._resource_name)

    def __repr__(self) -> str:
        return "<%r(%r)>" % (self.__class__.__name__, self._resource_name)

    def __enter__(self) -> "Resource":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    @property
    def last_status(self) -> constants.StatusCode:
        """Last status code for this session.

        :rtype: :class:`pyvisa.constants.StatusCode`
        """
        return self.visalib.get_last_status_in_session(self.session)

    def _cleanup_timeout(self, timeout: Optional[float]) -> int:
        if timeout is None or math.isinf(timeout):
            timeout = constants.VI_TMO_INFINITE

        elif timeout < 1:
            timeout = constants.VI_TMO_IMMEDIATE

        elif not (1 <= timeout <= 4294967294):
            raise ValueError("timeout value is invalid")

        else:
            timeout = int(timeout)

        return timeout

    @property
    def timeout(self) -> float:
        """The timeout in milliseconds for all resource I/O operations.

        Special values:

        - **immediate** (``VI_TMO_IMMEDIATE``): 0
          (for convenience, any value smaller than 1 is considered as 0)
        - **infinite** (``VI_TMO_INFINITE``): ``float('+inf')``
          (for convenience, None is considered as ``float('+inf')``)

        To set an **infinite** timeout, you can also use:

        >>> del instrument.timeout

        """
        timeout = self.get_visa_attribute(constants.ResourceAttribute.timeout_value)
        if timeout == constants.VI_TMO_INFINITE:
            return float("+inf")
        return timeout

    @timeout.setter
    def timeout(self, timeout: float) -> None:
        timeout = self._cleanup_timeout(timeout)
        self.set_visa_attribute(constants.ResourceAttribute.timeout_value, timeout)

    @timeout.deleter
    def timeout(self) -> None:
        self.set_visa_attribute(
            constants.ResourceAttribute.timeout_value, constants.VI_TMO_INFINITE
        )

    @property
    def resource_info(self) -> highlevel.ResourceInfo:
        """Get the extended information of this resource.

        """
        return self.visalib.parse_resource_extended(
            self._resource_manager.session, self._resource_name
        )[0]

    @property
    def interface_type(self) -> constants.InterfaceType:
        """The interface type of the resource as a number.

        """
        return self.visalib.parse_resource(
            self._resource_manager.session, self._resource_name
        )[0].interface_type

    def ignore_warning(self, *warnings_constants) -> ContextManager:
        """Ignoring warnings context manager for the current resource.

        :param warnings_constants: constants identifying the warnings to ignore.

        """
        return self.visalib.ignore_warning(self.session, *warnings_constants)

    def open(
        self,
        access_mode: constants.AccessModes = constants.AccessModes.no_lock,
        open_timeout: int = 5000,
    ) -> None:
        """Opens a session to the specified resource.

        :param access_mode: Specifies the mode by which the resource is to be accessed.
        :type access_mode: :class:`pyvisa.constants.AccessModes`
        :param open_timeout: If the ``access_mode`` parameter requests a lock, then this parameter specifies the
                             absolute time period (in milliseconds) that the resource waits to get unlocked before this
                             operation returns an error.
        :type open_timeout: int
        """

        logger.debug("%s - opening ...", self._resource_name, extra=self._logging_extra)
        with self._resource_manager.ignore_warning(constants.VI_SUCCESS_DEV_NPRESENT):
            self.session, status = self._resource_manager.open_bare_resource(
                self._resource_name, access_mode, open_timeout
            )

            if status == constants.VI_SUCCESS_DEV_NPRESENT:
                # The device was not ready when we opened the session.
                # Now it gets five seconds more to become ready.
                # Every 0.1 seconds we probe it with viClear.
                start_time = time.time()
                sleep_time = 0.1
                try_time = 5
                while time.time() - start_time < try_time:
                    time.sleep(sleep_time)
                    try:
                        self.clear()
                        break
                    except errors.VisaIOError as error:
                        if error.error_code != constants.VI_ERROR_NLISTENERS:
                            raise

        self._logging_extra["session"] = self.session
        logger.debug(
            "%s - is open with session %s",
            self._resource_name,
            self.session,
            extra=self._logging_extra,
        )

    def before_close(self) -> None:
        """Called just before closing an instrument.
        """
        self.__switch_events_off()

    def close(self) -> None:
        """Closes the VISA session and marks the handle as invalid.
        """
        try:
            logger.debug("%s - closing", self._resource_name, extra=self._logging_extra)
            self.before_close()
            self.visalib.close(self.session)
            logger.debug(
                "%s - is closed", self._resource_name, extra=self._logging_extra
            )
            # Mypy is confused by the idea that we can set a value we cannot get
            self.session = None  # type: ignore
        except errors.InvalidSession:
            pass

    def __switch_events_off(self) -> None:
        self.disable_event(constants.VI_ALL_ENABLED_EVENTS, constants.VI_ALL_MECH)
        self.discard_events(constants.VI_ALL_ENABLED_EVENTS, constants.VI_ALL_MECH)
        self.visalib.uninstall_all_visa_handlers(self.session)

    def get_visa_attribute(self, name: constants.ResourceAttribute) -> Any:
        """Retrieves the state of an attribute in this resource.

        :param name: Resource attribute for which the state query is made (see Attributes.*)
        :return: The state of the queried attribute for a specified resource.
        :rtype: unicode (Py2) or str (Py3), list or other type
        """
        return self.visalib.get_attribute(self.session, name)[0]

    def set_visa_attribute(
        self, name: constants.ResourceAttribute, state: Any
    ) -> constants.StatusCode:
        """Sets the state of an attribute.

        :param name: Attribute for which the state is to be modified. (Attributes.*)
        :param state: The state of the attribute to be set for the specified object.
        :return: return value of the library call.
        :rtype: :class:`pyvisa.constants.StatusCode`
        """
        return self.visalib.set_attribute(self.session, name, state)

    def clear(self) -> None:
        """Clears this resource.

        """
        self.visalib.clear(self.session)

    def install_handler(self, event_type, handler, user_handle=None) -> Any:
        """Installs handlers for event callbacks in this resource.

        :param event_type: Logical event identifier.
        :param handler: Interpreted as a valid reference to a handler to be installed by a client application.
        :param user_handle: A value specified by an application that can be used for identifying handlers
                            uniquely for an event type.
        :returns: user handle (a ctypes object)
        """

        return self.visalib.install_visa_handler(
            self.session, event_type, handler, user_handle
        )

    def uninstall_handler(self, event_type, handler, user_handle=None) -> None:
        """Uninstalls handlers for events in this resource.

        :param event_type: Logical event identifier.
        :param handler: Interpreted as a valid reference to a handler to be uninstalled by a client application.
        :param user_handle: The user handle (ctypes object or None) returned by install_handler.
        """

        self.visalib.uninstall_visa_handler(
            self.session, event_type, handler, user_handle
        )

    def disable_event(self, event_type, mechanism) -> None:
        """Disables notification of the specified event type(s) via the specified mechanism(s).

        :param event_type: Logical event identifier.
        :param mechanism: Specifies event handling mechanisms to be disabled.
                          (Constants.VI_QUEUE, .VI_HNDLR, .VI_SUSPEND_HNDLR, .VI_ALL_MECH)
        """
        self.visalib.disable_event(self.session, event_type, mechanism)

    def discard_events(self, event_type, mechanism) -> None:
        """Discards event occurrences for specified event types and mechanisms in this resource.

        :param event_type: Logical event identifier.
        :param mechanism: Specifies event handling mechanisms to be discarded.
                          (Constants.VI_QUEUE, .VI_HNDLR, .VI_SUSPEND_HNDLR, .VI_ALL_MECH)
        """
        self.visalib.discard_events(self.session, event_type, mechanism)

    def enable_event(self, event_type, mechanism, context=None) -> None:
        """Enable event occurrences for specified event types and mechanisms in this resource.

        :param event_type: Logical event identifier.
        :param mechanism: Specifies event handling mechanisms to be enabled.
                          (Constants.VI_QUEUE, .VI_HNDLR, .VI_SUSPEND_HNDLR)
        :param context:  Not currently used, leave as None.
        """
        self.visalib.enable_event(self.session, event_type, mechanism, context)

    def wait_on_event(
        self, in_event_type, timeout, capture_timeout: bool = False
    ) -> WaitResponse:
        """Waits for an occurrence of the specified event in this resource.

        :param in_event_type: Logical identifier of the event(s) to wait for.
        :param timeout: Absolute time period in time units that the resource shall wait for a specified event to
                        occur before returning the time elapsed error. The time unit is in milliseconds.
                        None means waiting forever if necessary.
        :param capture_timeout: When True will not produce a VisaIOError(VI_ERROR_TMO) but
                                instead return a WaitResponse with timed_out=True
        :return: A WaitResponse object that contains event_type, context and ret value.
        """
        try:
            event_type, context, ret = self.visalib.wait_on_event(
                self.session, in_event_type, timeout
            )
        except errors.VisaIOError as exc:
            if capture_timeout and exc.error_code == constants.StatusCode.error_timeout:
                return WaitResponse(
                    in_event_type, None, exc.error_code, self.visalib, timed_out=True
                )
            raise
        return WaitResponse(event_type, context, ret, self.visalib)

    def lock(
        self, timeout: Union[float, Literal["default"]] = "default", requested_key=None
    ):
        """Establish a shared lock to the resource.

        :param timeout: Absolute time period (in milliseconds) that a resource
                        waits to get unlocked by the locking session before
                        returning an error. (Defaults to self.timeout)
        :param requested_key: Access key used by another session with which you
                              want your session to share a lock or None to generate
                              a new shared access key.
        :returns: A new shared access key if requested_key is None,
                  otherwise, same value as the requested_key

        """
        tout = cast(float, self.timeout if timeout == "default" else timeout)
        clean_timeout = self._cleanup_timeout(tout)
        return self.visalib.lock(
            self.session, constants.Lock.shared, clean_timeout, requested_key
        )[0]

    def lock_excl(self, timeout: Union[float, Literal["default"]] = "default") -> None:
        """Establish an exclusive lock to the resource.

        :param timeout: Absolute time period (in milliseconds) that a resource
                        waits to get unlocked by the locking session before
                        returning an error. (Defaults to self.timeout)

        """
        tout = cast(float, self.timeout if timeout == "default" else timeout)
        clean_timeout = self._cleanup_timeout(tout)
        self.visalib.lock(self.session, constants.Lock.exclusive, clean_timeout, None)

    def unlock(self) -> None:
        """Relinquishes a lock for the specified resource.

        """
        self.visalib.unlock(self.session)

    @contextlib.contextmanager
    def lock_context(
        self,
        timeout: Union[float, Literal["default"]] = "default",
        requested_key="exclusive",
    ) -> Iterator:
        """A context that locks

        :param timeout: Absolute time period (in milliseconds) that a resource
                        waits to get unlocked by the locking session before
                        returning an error. (Defaults to self.timeout)
        :param requested_key: When using default of 'exclusive' the lock
                              is an exclusive lock.
                              Otherwise it is the access key for the shared lock or
                              None to generate a new shared access key.

        The returned context is the access_key if applicable.

        """
        if requested_key == "exclusive":
            self.lock_excl(timeout)
            access_key = None
        else:
            access_key = self.lock(timeout, requested_key)
        try:
            yield access_key
        finally:
            self.unlock()


Resource.register(constants.InterfaceType.unknown, "")(Resource)
