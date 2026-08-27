"""SOLEIL Proxima 2A CATS sample changer.

Talks to the CATS Tango DS via channels/commands declared in the YAML
configuration (see ``webconfig/singleton_objects/sample_changer.yaml``).
This class also exposes the CATS *maintenance* surface (power, lids,
trajectories) consumed by ``SOLEILCatsMaint`` — which is now a thin
delegating proxy.
"""

import logging
import time
import traceback

import gevent

from mxcubecore import HardwareRepository as HWR
from mxcubecore.HardwareObjects.Cats90 import (
    BASKET_SPINE,
    BASKET_UNIPUCK,
    Basket,
    Cats90,
    Pin,
    SpineBasket,
    TOOL_DOUBLE_GRIPPER,
    TOOL_SPINE,
    TOOL_UNIPUCK,
    UnipuckBasket,
)
from mxcubecore.HardwareObjects.abstract.AbstractSampleChanger import (
    SampleChangerState,
)
from mxcubecore.TaskUtils import task


class SoleilPuck(Basket):
    def __init__(self, container, number, samples_num=16, name="UniPuck", parent=None):
        super().__init__(container, number, samples_num=samples_num, name=name)
        self.parent = parent
        if self.parent is not None:
            for slot in self.get_components():
                self.parent.component_by_address[slot.get_address()] = slot


class SOLEILCats(Cats90):
    """SOLEIL Proxima 2A CATS sample changer (12 baskets, 3 lids, UniPuck)."""

    __TYPE__ = "CATS"

    default_no_lids = 3
    baskets_per_lid = 3
    default_samples_per_basket = 16
    default_no_of_baskets = 9
    default_basket_type = BASKET_UNIPUCK
    DETECT_PUCKS = True
    default_soak_lid = 2

    # Maps UI-facing command names (returned by get_cmd_info / passed to
    # send_command) to the framework command attribute set up from YAML.
    UI_COMMAND_MAP = {
        "powerOn": "_cmdPowerOn",
        "powerOff": "_cmdPowerOff",
        "regulon": "_cmdRegulOn",
        "reguloff": "_cmdRegulOff",
        "openlid1": "_cmdOpenLid1",
        "closelid1": "_cmdCloseLid1",
        "openlid2": "_cmdOpenLid2",
        "closelid2": "_cmdCloseLid2",
        "openlid3": "_cmdOpenLid3",
        "closelid3": "_cmdCloseLid3",
        "home": "_cmdHome",
        "drysoak": "_cmdDrySoak",
        "dryht": "_cmdDryHt",
        "back": "_cmdBack",
        "safe": "_cmdSafe",
        "abort": "_cmdAbort",
        "reset": "_cmdReset",
        "clear_memory": "_cmdClearMemory",
    }

    # Channel name -> handler method name. Channels not in this map are
    # retrieved but not auto-connected to a handler.
    CHANNEL_HANDLERS = {
        "_chnState": "_update_state",
        "_chnPowered": "_update_powered_state",
        "_chnPathRunning": "_update_running_state",
        "_chnSampleBarcode": "_update_barcode",
        "_chnAllLidsClosed": "_update_global_state",
        "_chnMessage": "_update_message",
        "_chnLN2Regulation": "_update_regulation_state",
        "_chnCurrentTool": "_update_tool_state",
        "_chnLid1State": "_update_lid1_state",
        "_chnLid2State": "_update_lid2_state",
        "_chnLid3State": "_update_lid3_state",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.component_by_address = {}
        self.basket_channels = []
        self.basket_presence = []
        # Strong references to the channel "update" slots. The dispatcher holds
        # receivers weakly (weak=True), so a freshly-built wrapped closure passed
        # straight to connect_signal would be garbage-collected right after init,
        # silently killing every polled update. Keep them alive here.
        self._channel_slots = {}
        # Connection-health flag. Flipped by handlers and probes:
        #   "UNKNOWN" — pre-init / not yet probed
        #   "ONLINE"  — last read/probe succeeded
        #   "OFFLINE" — last read/probe failed (PyCATS or Tango)
        self._connection_state = "UNKNOWN"

    def init(self):
        # Bookkeeping defaults
        self._selected_sample = None
        self._selected_basket = None
        self._scIsCharging = None
        self.read_datamatrix = False
        self.unipuck_tool = TOOL_UNIPUCK
        self.former_loaded = None
        self.cats_device = None
        self.cats_datamatrix = ""
        self.cats_loaded_lid = None
        self.cats_loaded_num = None
        self.cats_powered = False
        self.cats_status = ""
        self.cats_running = False
        self.cats_state = "Unknown"
        self.cats_lids_closed = False
        self._toolopen = None
        self._powered = None
        self._running = None
        self._regulating = None
        self._lid1state = None
        self._lid2state = None
        self._lid3state = None
        self._message = None
        # Translated Tango DevState (see _translate_state); drives the pill.
        self._sc_state = SampleChangerState.Unknown

        # Configuration
        self.tangoname = self.get_property("tangoname")
        self.no_of_lids = self.get_property("no_of_lids", self.default_no_lids)
        self.no_of_baskets = self.get_property(
            "no_of_baskets", self.default_no_of_baskets
        )
        self.samples_per_basket = self.get_property(
            "samples_per_basket", self.default_samples_per_basket
        )
        self.do_detect_pucks = self.get_property("detect_pucks", SOLEILCats.DETECT_PUCKS)
        self.use_update_timer = False
        self.soak_lid = self.get_property("no_soak_lid", self.default_soak_lid)
        self.cats_model = "CATS"
        self.basket_types = [None] * self.no_of_baskets
        self.basket_presence = [True] * self.no_of_baskets
        self.basket_channels = []

        self._setup_channels_from_yaml()
        self._setup_commands_from_yaml()
        self._init_sc_contents()
        # Populate the dewar contents once from the basket-presence Tango
        # channels so the Content tree and get_sample_list() are not empty
        # (get_contents_as_dict only emits *present* elements). Then push the
        # computed state so the Equipment status pill shows the live value
        # instead of a stuck UNKNOWN — the web adapter connects to
        # `stateChanged` after init, and the base _set_state only emits on
        # change.
        self._do_update_cats_contents()
        self._update_global_state()

        if self.get_property("read_datamatrix"):
            self.set_read_barcode(True)

        unipuck_tool = self.get_property("unipuck_tool")
        if unipuck_tool is not None:
            try:
                self.set_unipuck_tool(int(unipuck_tool))
            except (TypeError, ValueError):
                pass

        # Final smoke test: confirm Tango DS responds. Raises if not.
        self._probe_tango_connection()

    # ------------------------------------------------------------------
    # YAML channel/command wiring
    # ------------------------------------------------------------------

    def _setup_channels_from_yaml(self):
        """Retrieve YAML-declared channels, connect handlers, and seed
        their initial values.
        """
        log = logging.getLogger("HWR")

        for channel_name, handler_name in self.CHANNEL_HANDLERS.items():
            channel = self.get_channel_object(channel_name, optional=True)
            if channel is None:
                log.warning(
                    "SOLEILCats: channel %s missing from YAML; skipping handler %s",
                    channel_name,
                    handler_name,
                )
                continue
            setattr(self, channel_name, channel)
            handler = getattr(self, handler_name, None)
            if handler is None:
                log.warning(
                    "SOLEILCats: no handler %s for channel %s",
                    handler_name,
                    channel_name,
                )
                continue
            try:
                handler(channel.get_value())
            except Exception as exc:
                log.exception(
                    "SOLEILCats: initial read failed for %s", channel_name
                )
                self._set_connection_state(
                    "OFFLINE", "%s read failed: %s" % (channel_name, exc)
                )
            # Retain a strong ref to the wrapped closure (see __init__): the
            # dispatcher stores receivers weakly, so an inline closure would be
            # collected after init and no update would ever be delivered.
            wrapped = self._wrap_handler(channel_name, handler)
            self._channel_slots[channel_name] = wrapped
            channel.connect_signal("update", wrapped)

        # Loaded-sample channels: drive `_update_loaded_sample` so a load/unload
        # completing on the arm auto-publishes `loadedSampleChanged` (the web
        # adapter clears/sets the table highlight and dismisses the operation
        # dialog). The handler is idempotent, so the explicit calls in
        # load()/unload() stay harmless. Same strong-ref wrapping as the other
        # channels — the dispatcher holds receivers weakly.
        for name in ("_chnNumLoadedSample", "_chnLidLoadedSample"):
            channel = self.get_channel_object(name, optional=True)
            if channel is None:
                log.warning("SOLEILCats: channel %s missing from YAML", name)
                continue
            setattr(self, name, channel)
            wrapped = self._wrap_handler(name, self._update_loaded_sample)
            self._channel_slots[name] = wrapped
            channel.connect_signal("update", wrapped)

        # Basket-presence channels — collected, connected only if pucks
        # are being detected.
        for index in range(1, self.no_of_baskets + 1):
            channel_name = "_chnBasket%dState" % index
            channel = self.get_channel_object(channel_name, optional=True)
            if channel is None:
                log.warning(
                    "SOLEILCats: basket channel %s missing from YAML", channel_name
                )
                continue
            setattr(self, channel_name, channel)
            self.basket_channels.append(channel)

        if self.do_detect_pucks:
            for channel in self.basket_channels:
                channel.connect_signal("update", self.cats_basket_presence_changed)

    def _setup_commands_from_yaml(self):
        log = logging.getLogger("HWR")
        # All CMD_NAMES referenced by SOLEILCats methods or by send_command.
        cmd_names = (
            "_cmdLoad",
            "_cmdUnload",
            "_cmdChainedLoad",
            "_cmdAbort",
            "_cmdScanSample",
            "_cmdPowerOn",
            "_cmdPowerOff",
            "_cmdRegulOn",
            "_cmdRegulOff",
            "_cmdReset",
            "_cmdBack",
            "_cmdSafe",
            "_cmdHome",
            "_cmdDry",
            "_cmdDrySoak",
            "_cmdDryHt",
            "_cmdSoak",
            "_cmdResetParameters",
            "_cmdClearMemory",
            "_cmdAckSampleMemory",
            "_cmdOpenTool",
            "_cmdCloseTool",
            "_cmdToolCal",
            "_cmdOpenLid1",
            "_cmdCloseLid1",
            "_cmdOpenLid2",
            "_cmdCloseLid2",
            "_cmdOpenLid3",
            "_cmdCloseLid3",
            "_cmdResetMotion",
            "_cmdRecoverFailure",
            "_cmdCalibration",
            "_cmdSetOnDiff",
            "_cmdMagnetOn",
            "_cmdMagnetOff",
            "_cmdToolOpen",
            "_cmdToolClose",
        )
        for name in cmd_names:
            cmd = self.get_command_object(name)
            if cmd is None:
                log.warning("SOLEILCats: command %s missing from YAML", name)
            setattr(self, name, cmd)

    # ------------------------------------------------------------------
    # Connection-health tracking
    # ------------------------------------------------------------------

    def _set_connection_state(self, new_state, detail=""):
        """Update the connection-health flag and emit a signal on change."""
        if new_state == self._connection_state:
            return
        self._connection_state = new_state
        logging.getLogger("HWR").warning(
            "SOLEILCats: connection state -> %s (%s)", new_state, detail
        )
        try:
            self.emit("connectionStateChanged", (new_state, detail))
        except Exception:
            logging.getLogger("HWR").exception(
                "SOLEILCats: failed to emit connectionStateChanged"
            )

    def _probe_tango_connection(self):
        """One-shot Tango health probe. Raises if the CATS DS is unreachable.

        Called at the end of init() so a downed DS makes mxcube startup fail
        loudly with a clear error pointing at the SC, instead of degrading
        silently into "no state ever changes".
        """
        chn = getattr(self, "_chnState", None)
        if chn is None:
            self._set_connection_state(
                "OFFLINE", "channel _chnState not configured in YAML"
            )
            raise RuntimeError(
                "SOLEILCats: _chnState channel missing — check YAML and Tango DS"
            )
        try:
            value = chn.get_value()
        except Exception as exc:
            self._set_connection_state(
                "OFFLINE", "Tango read failed: %s" % exc
            )
            raise RuntimeError(
                "SOLEILCats: Tango DS unreachable (read of _chnState failed: %s)"
                % exc
            ) from exc
        self._set_connection_state(
            "ONLINE", "Tango DS responsive (state=%s)" % value
        )

    def _wrap_handler(self, channel_name, handler):
        """Wrap a channel update handler so successful invocations refresh
        the ONLINE state and exceptions flip OFFLINE — without dropping
        the update.
        """

        def wrapped(value):
            try:
                handler(value)
            except Exception as exc:
                logging.getLogger("HWR").exception(
                    "SOLEILCats: handler for %s raised", channel_name
                )
                self._set_connection_state(
                    "OFFLINE",
                    "%s update raised: %s" % (channel_name, exc),
                )
                return
            if self._connection_state != "ONLINE":
                self._set_connection_state(
                    "ONLINE", "channel %s recovered" % channel_name
                )

        return wrapped

    def check_connection(self):
        """Public health probe. Returns ``(is_ok: bool, detail: str)``.

        Safe to call from the maintenance UI or a watchdog. Performs a single
        Tango read of ``_chnState`` and updates ``_connection_state``.
        """
        chn = getattr(self, "_chnState", None)
        if chn is None:
            self._set_connection_state(
                "OFFLINE", "channel _chnState not configured"
            )
            return False, "channel _chnState not configured"
        try:
            chn.get_value()
        except Exception as exc:
            self._set_connection_state("OFFLINE", str(exc))
            return False, str(exc)
        self._set_connection_state("ONLINE", "")
        return True, "OK"

    # ------------------------------------------------------------------
    # Basket / sample bookkeeping
    # ------------------------------------------------------------------

    def get_basket_list(self):
        basket_list = []
        for basket in self.get_components():
            if isinstance(basket, Basket):
                basket_list.append(basket)
        return basket_list

    def _get_by_address(self, address):
        try:
            return self.component_by_address[address]
        except KeyError:
            component = self.get_component_by_address(address)
            self.component_by_address[address] = component
            return component

    def _init_sc_contents(self):
        """Initialise sample-changer contents with default values."""
        start = time.time()
        logging.getLogger("HWR").info("SOLEILCats: initialising contents")

        for i in range(self.no_of_baskets):
            if self.basket_types[i] == BASKET_SPINE:
                basket = SpineBasket(self, i + 1)
            elif self.basket_types[i] == BASKET_UNIPUCK:
                basket = UnipuckBasket(self, i + 1)
            else:
                basket = SoleilPuck(
                    self,
                    i + 1,
                    samples_num=self.samples_per_basket,
                    parent=self,
                )
            self._add_component(basket)
            self.component_by_address[basket.get_address()] = basket

        for basket_index in range(self.no_of_baskets):
            basket = self.get_components()[basket_index]
            basket._set_info(False, None, False)

        for basket in self.get_components():
            for sample in basket.get_components():
                sample._set_info(False, None, False)
                sample._set_loaded(False, False)
                sample._set_holder_length(Pin.STD_HOLDERLENGTH)

        logging.getLogger("HWR").info(
            "SOLEILCats: contents initialised in %.3fs", time.time() - start
        )

    def _do_update_cats_contents(self):
        for basket_index in range(self.no_of_baskets):
            if self.do_detect_pucks:
                channel = self.basket_channels[basket_index]
                is_present = channel.get_value()
            else:
                is_present = True
            self.basket_presence[basket_index] = is_present
        self._update_cats_contents()

    def _update_cats_contents(self):
        start = time.time()
        logging.getLogger("HWR").info(
            "SOLEILCats: updating contents %s", self.basket_presence
        )
        for basket_index in range(self.no_of_baskets):
            basket = self.get_components()[basket_index]
            is_present = self.basket_presence[basket_index]
            if is_present is None:
                continue
            if is_present ^ basket.is_present():
                datamatrix = None
                basket._set_info(is_present, datamatrix, False)
                for sample_index in range(basket.get_number_of_samples()):
                    address = Pin.get_sample_address(
                        basket_index + 1, sample_index + 1
                    )
                    sample = self._get_by_address(address)
                    present = sample.get_container().is_present()
                    matrix = "          " if present else None
                    sample._set_info(present, matrix, False)
                    sample._set_loaded(False, False)

        self._trigger_contents_updated_event()
        self._update_loaded_sample()
        logging.getLogger("HWR").debug(
            "SOLEILCats: _update_cats_contents took %.3fs", time.time() - start
        )

    # ------------------------------------------------------------------
    # Power / load / unload
    # ------------------------------------------------------------------

    def load(self, sample=None, wait=True):
        self._update_state()
        logging.getLogger().info("SOLEILCats: load")
        self.assert_not_charging()

        # `sample` arrives as a container address string ("basket:sample",
        # e.g. "1:01") from the web adapter / queue, or as a Pin component.
        # Resolve it to the registered component and read its basket/vial —
        # the same path the base Cats90.load uses (no custom separator).
        component = self._resolve_component(sample)
        if component is None:
            raise Exception("SOLEILCats: no sample selected to load")
        puck = component.get_basket_no()
        sampleno = component.get_vial_no()
        logging.getLogger("HWR").info(
            "SOLEILCats: load component %s", component.get_address()
        )

        lid, sample_in_lid = self.basketsample_to_lidsample(puck, sampleno)
        tool = self.tool_for_basket(puck)
        stype = self.get_cassette_type(puck)
        # CATS DS `getput` argin: [tool, lid, sample, type, newmode,
        # xshift, yshift, zshift]. Sent to the Tango command declared in the
        # YAML — no external socket. (NOTE: argin layout confirmed against
        # the canonical Cats90._do_load; verify on the beamline vs the DS.)
        argin = [
            str(int(tool)),
            str(int(lid)),
            str(int(sample_in_lid)),
            str(int(stype)),
            "0",
            "0",
            "0",
            "0",
        ]
        # Choose the CATS operation the same way the base Cats90._do_load
        # does: a plain put (`_cmdLoad`) when the goniometer is empty, and a
        # sample exchange (`_cmdChainedLoad`, unmount-then-mount) when one is
        # already mounted. `has_loaded_sample()` delegates to the
        # diffractometer's `SampleIsLoaded` — the authority on what is on the
        # gonio. The argin is identical for both commands; only the command
        # object differs.
        if self.has_loaded_sample():
            command = self._cmdChainedLoad
            operation = "chained load"
        else:
            command = self._cmdLoad
            operation = "load"
        logging.getLogger("HWR").info(
            "SOLEILCats: %s puck=%d sample=%d argin=%s",
            operation,
            puck,
            sampleno,
            argin,
        )
        # Only send the command while the arm is idle. If a path is already
        # running the CATS DS would reject a second command (and the first is
        # already doing the work), so do nothing.
        if self._is_device_moving():
            logging.getLogger("HWR").warning(
                "SOLEILCats: device moving — skipping %s", operation
            )
            return None
        result = self._execute_server_task(command, argin)
        # Publish the new loaded sample. Idempotent — also fires from the
        # loaded-sample channel update — but doing it here guarantees the
        # change is out before we return. `_mount_sample` needs a truthy
        # return to start autoloop centring and run its post-mount cleanup.
        self._update_loaded_sample()
        return result

    def wash(self, wait=True):
        """Unmount and re-mount the currently loaded pin.

        `load` already issues `_cmdChainedLoad` (CATS `getput`) whenever
        something is on the goniometer, and — unlike Cats90 — has no
        "already loaded" guard, so a wash is a chained load of the mounted
        address. The base Cats90.wash() cannot be used here: it calls
        `_execute_task` with the 4-argument AbstractSampleChanger signature,
        while this class overrides it with a 3-argument one.
        """
        component = self.get_loaded_sample()
        if component is None:
            raise Exception("SOLEILCats: cannot wash, no sample mounted")
        logging.getLogger("HWR").info("SOLEILCats: wash %s", component.get_address())
        return self.load(component, wait=wait)

    def unload(self, sample_slot=None, wait=True):
        logging.getLogger().info("SOLEILCats: unload")
        self.assert_not_charging()

        loaded_lid = self._chnLidLoadedSample.get_value()
        loaded_num = self._chnNumLoadedSample.get_value()
        if loaded_lid in (None, -1):
            logging.getLogger("HWR").warning(
                "SOLEILCats: unload — no sample mounted (lid=%s)", loaded_lid
            )
            return
        loaded_basket, _ = self.lidsample_to_basketsample(loaded_lid, loaded_num)
        tool = self.tool_for_basket(loaded_basket)
        # CATS DS `get` argin: [tool, newmode, xshift, yshift, zshift].
        argin = [str(int(tool)), "0", "0", "0", "0"]
        logging.getLogger("HWR").info("SOLEILCats: unload argin=%s", argin)
        # Only send while the arm is idle; a running path would reject the
        # command, so do nothing.
        if self._is_device_moving():
            logging.getLogger("HWR").warning(
                "SOLEILCats: device moving — skipping unload"
            )
            return
        self._execute_server_task(self._cmdUnload, argin)
        # Publish the transition to "no sample" so the web adapter clears the
        # loaded sample and dismisses the "Sample changer in operation"
        # dialog. Idempotent; also fires from the loaded-sample channel.
        self._update_loaded_sample()

    def _update_loaded_sample(self, *args):
        """Publish the currently mounted sample when it changes.

        Called both from the loaded-sample channel updates and explicitly after
        load/unload. ``get_loaded_sample`` is overridden here to read the CATS
        channels directly, so a change can't be detected by re-reading it for
        both sides of a comparison — instead we compare the freshly-read current
        sample against the last *published* one (``self.former_loaded``). Emits
        ``loadedSampleChanged`` / ``infoChanged`` only on a real transition, so
        the channel-driven and explicit call paths are both idempotent.

        Any positional args (the channel value passed by the dispatcher) are
        ignored; the loaded lid/num are always read fresh.
        """
        loaded_num = self._chnNumLoadedSample.get_value()
        loaded_lid = self._chnLidLoadedSample.get_value()
        self.cats_loaded_lid = loaded_lid
        self.cats_loaded_num = loaded_num

        if None in (loaded_lid, loaded_num) or -1 in (loaded_lid, loaded_num):
            current = None
        else:
            basket, sample = self.lidsample_to_basketsample(loaded_lid, loaded_num)
            current = self._get_by_address(Pin.get_sample_address(basket, sample))

        previous = self.former_loaded
        same_address = (
            previous is not None
            and current is not None
            and previous.get_address() == current.get_address()
        )
        if current is previous or same_address:
            return

        if previous is not None:
            previous._set_loaded(False, True)
        if current is not None:
            current._set_loaded(True, True)
        self.former_loaded = current

        address = current.get_address() if current is not None else "None"
        logging.getLogger("HWR").info("SOLEILCats: loaded sample %s", address)
        self._trigger_loaded_sample_changed_event(current)
        self._trigger_info_changed_event()

    def cats_state_changed(self, value=None):
        logging.debug("SOLEILCats: state_changed %s", value)
        self.cats_state = value
        self._update_state()

    def has_loaded_sample(self):
        # "Is a sample mounted on the goniometer?" is the diffractometer's
        # knowledge — it owns the MD Exporter and its `SampleIsLoaded` channel.
        # Delegating keeps the changer from opening a second px2em connection.
        diffractometer = HWR.beamline.diffractometer
        if diffractometer is None:
            logging.getLogger("HWR").warning(
                "SOLEILCats: diffractometer not available for sample detection"
            )
            return False
        return diffractometer.is_sample_loaded()

    def get_loaded_sample(self, puck=None, sample=None):
        if puck is None or sample is None:
            loaded_num = int(self._chnNumLoadedSample.get_value())
            loaded_lid = int(self._chnLidLoadedSample.get_value())
            if -1 in (loaded_num, loaded_lid):
                return None
            puck, sample = self.lidsample_to_basketsample(loaded_lid, loaded_num)
        address = Pin.get_sample_address(puck, sample)
        return self.get_component_by_address(address)

    def assert_not_charging(self):
        if self.cats_running:
            raise Exception("Sample Changer is in Charging mode")

    def _effective_basket_type(self, basketno):
        """Return the puck type for ``basketno`` (1-based).

        SOLEIL does not read ``CassetteType`` from the CATS DS, so
        ``basket_types`` stays ``[None, ...]`` (which drives the SoleilPuck
        container tree in ``_init_sc_contents``). Fall back to the
        ``default_basket_type`` (UniPuck) so tool/type resolution does not
        collapse to -1 and get rejected by the DS on load/unload.
        """
        basket_type = self.basket_types[basketno - 1]
        if basket_type is None:
            basket_type = self.default_basket_type
        return basket_type

    def tool_for_basket(self, basketno):
        if self._effective_basket_type(basketno) == BASKET_SPINE:
            tool = TOOL_SPINE
        else:
            tool = self.unipuck_tool
        logging.getLogger("HWR").debug(
            "SOLEILCats: tool for basket %s is %s", basketno, tool
        )
        return tool

    def get_cassette_type(self, basketno):
        if self.is_isara():
            return 1
        if self._effective_basket_type(basketno) == BASKET_SPINE:
            return 0
        return 1

    # ------------------------------------------------------------------
    # MAINTENANCE TRAJECTORIES — exposed via SOLEILCatsMaint proxy
    # ------------------------------------------------------------------

    def back_traj(self):
        return self._execute_task(False, self._do_back)

    def safe_traj(self):
        return self._execute_task(False, self._do_safe)

    def _do_abort(self):
        self._cmdAbort()

    def _do_home(self):
        self._cmdHome(self.get_current_tool())

    def _do_reset(self):
        logging.getLogger("HWR").debug("SOLEILCats: reset (no-op)")
        return

    def _do_reset_memory(self):
        self._cmdClearMemory()
        gevent.sleep(1)
        self._cmdResetParameters()
        gevent.sleep(1)

    def _do_reset_motion(self):
        self._cmdResetMotion()

    def _do_recover_failure(self):
        self._cmdRecoverFailure()

    def _do_calibration(self):
        self._cmdCalibration([self.get_current_tool()])

    def _do_open_tool(self):
        self._cmdOpenTool()

    def _do_close_tool(self):
        self._cmdCloseTool()

    def _do_dry_gripper(self):
        self._cmdDrySoak([str(self.get_current_tool()), str(self.soak_lid)])

    def _do_dry_ht(self):
        # Room-temperature dry & soak. The CATS DS `dry_ht` trajectory takes
        # no arguments; its duration is covered by the command timeout
        # declared in the YAML, not by _execute_server_task.
        self._cmdDryHt()

    def _do_set_on_diff(self, sample):
        if sample is None:
            raise Exception("No sample selected")
        parts = str(sample).split(":")
        lid = (int(parts[0]) - 1) // 3 + 1
        puc_pos = ((int(parts[0]) - 1) % 3) * 10 + int(parts[1])
        argin = [str(lid), str(puc_pos), "0"]
        logging.getLogger().info("SOLEILCats: SetOnDiff %s", argin)
        self._execute_server_task(self._cmdSetOnDiff, argin)

    def _do_back(self):
        argin = [str(self.get_current_tool()), "0"]
        self._execute_server_task(self._cmdBack, argin)

    def _do_safe(self):
        self._execute_server_task(self._cmdSafe, self.get_current_tool())

    def _do_power_state(self, state=False):
        if state:
            self._cmdPowerOn()
        else:
            self._cmdPowerOff()

    def _do_enable_regulation(self):
        self._cmdRegulOn()

    def _do_disable_regulation(self):
        self._cmdRegulOff()

    def _do_lid1_state(self, state=True):
        cmd = self._cmdOpenLid1 if state else self._cmdCloseLid1
        self._execute_server_task(cmd)

    def _do_lid2_state(self, state=True):
        cmd = self._cmdOpenLid2 if state else self._cmdCloseLid2
        self._execute_server_task(cmd)

    def _do_lid3_state(self, state=True):
        cmd = self._cmdOpenLid3 if state else self._cmdCloseLid3
        self._execute_server_task(cmd)

    def _do_magnet_on(self):
        self._execute_server_task(self._cmdMagnetOn)

    def _do_magnet_off(self):
        self._execute_server_task(self._cmdMagnetOff)

    def _do_tool_open(self):
        self._execute_server_task(self._cmdToolOpen)

    def _do_tool_close(self):
        self._execute_server_task(self._cmdToolClose)

    # ------------------------------------------------------------------
    # PROTECTED / PRIVATE
    # ------------------------------------------------------------------

    def _execute_task(self, wait, method, *args):
        ret = self._run(method, wait=False, *args)
        if wait:
            return ret.get()
        return ret

    @task
    def _run(self, method, *args):
        try:
            return method(*args)
        except Exception:
            raise

    def _update_running_state(self, value):
        self._running = value
        self.emit("runningStateChanged", (value,))
        self._update_global_state()

    def _update_powered_state(self, value):
        self._powered = value
        self.emit("powerStateChanged", (value,))
        self._update_global_state()

    def _update_tool_state(self, value):
        self._toolopen = value
        self.emit("toolStateChanged", (value,))
        self._update_global_state()

    def _update_message(self, value):
        self._message = value
        self.emit("messageChanged", (value,))
        self._update_global_state()

    def _update_regulation_state(self, value):
        # Coerced to a real bool: get_global_state gates the regulon/reguloff
        # pair with `is True` / `is False` so a not-yet-read None disables
        # both, and a numpy bool from the Tango layer would defeat that.
        self._regulating = None if value is None else bool(value)
        self.emit("regulationStateChanged", (value,))
        self._update_global_state()

    def _update_barcode(self, value):
        self._barcode = value
        self.emit("barcodeChanged", (value,))

    def _update_state(self, value=None, value2=None):
        logging.debug("SOLEILCats: _update_state %s %s", value, value2)
        self._state = value
        self._sc_state = self._translate_state(value)
        self._update_global_state()

    # Tango DevState name -> AbstractSampleChanger enum. Mirrors the merged
    # SOLEILMicrodiffMotor `_motor_state_to_hwstate` pattern: a Tango `State`
    # attribute read yields a PyTango.DevState whose str() is unreliable, so we
    # key on `.name`. The DS State already encodes power-off (DISABLE/OFF), so
    # the pill no longer depends on the separate `_powered` flag.
    _DEVSTATE_TO_SC = {
        "ON": SampleChangerState.Ready,
        "STANDBY": SampleChangerState.Ready,
        "RUNNING": SampleChangerState.Moving,
        "MOVING": SampleChangerState.Moving,
        "DISABLE": SampleChangerState.Disabled,
        "OFF": SampleChangerState.Disabled,
        "INIT": SampleChangerState.Disabled,
        "ALARM": SampleChangerState.Alarm,
        "FAULT": SampleChangerState.Fault,
        "UNKNOWN": SampleChangerState.Unknown,
    }

    def _translate_state(self, raw):
        """Translate a raw Tango ``State`` value to a ``SampleChangerState``.

        Accepts a ``PyTango.DevState`` (uses ``.name``) or a plain string;
        unrecognised values fall back to ``Unknown``.
        """
        if raw is None:
            return SampleChangerState.Unknown
        if hasattr(raw, "name"):
            raw = raw.name
        return self._DEVSTATE_TO_SC.get(
            str(raw).upper(), SampleChangerState.Unknown
        )

    def _update_lid1_state(self, value):
        self._update_lid_state(1, value)

    def _update_lid2_state(self, value):
        self._update_lid_state(2, value)

    def _update_lid3_state(self, value):
        self._update_lid_state(3, value)

    def _update_lid_state(self, index, value):
        setattr(self, "_lid%dstate" % index, value)
        self.emit("lid%dStateChanged" % index, (value,))
        self._update_global_state()

    def _update_basket_state(self, index, value):
        self.emit("basket%dStateChanged" % index, (value,))
        self._update_global_state()

    def _update_operation_mode(self, value):
        self._charging = not value

    def _update_global_state(self, *args):
        state_dict, cmd_state, message = self.get_global_state()
        self.emit("globalStateChanged", (state_dict, cmd_state, message))
        self._sync_base_state(state_dict["state"])
        # Re-emit the base state unconditionally (mirroring globalStateChanged).
        # The web adapter connects to `stateChanged` after this object is
        # initialised and the base `_set_state` only emits on change, so without
        # this the Equipment status pill would stay stuck at the value read at
        # page load instead of tracking the live state.
        self.emit("stateChanged", (self.state, self.state))

    # Map of the computed global-state string to the AbstractSampleChanger enum.
    _GLOBAL_STATE_TO_SC_STATE = {
        "READY": SampleChangerState.Ready,
        "MOVING": SampleChangerState.Moving,
        "DISABLED": SampleChangerState.Disabled,
        "OFFLINE": SampleChangerState.Fault,
        "UNKNOWN": SampleChangerState.Unknown,
    }

    def _sync_base_state(self, state_str):
        """Mirror the computed global state onto the base state/status.

        SOLEILCats routes the real state through ``globalStateChanged`` (the
        maintenance panel) but never updated the AbstractSampleChanger
        ``state``/``status`` fields, so the web adapter's SC state pill stayed
        ``UNKNOWN`` (it reads ``get_status()`` and the ``stateChanged`` signal).
        Keeping them in sync here lights up both paths; unrecognised strings
        still fall back to ``Unknown``.
        """
        base_state = self._GLOBAL_STATE_TO_SC_STATE.get(
            state_str, SampleChangerState.Unknown
        )
        self._set_state(base_state, state_str)

    def get_global_state(self):
        """Snapshot of state, command-availability flags, and message."""
        offline = self._connection_state == "OFFLINE"
        # Operable state is derived from the translated Tango DevState, not from
        # a raw str() compare nor the separate `_powered` flag (the DevState
        # already reports DISABLE/OFF when unpowered).
        ready = (not offline) and self._sc_state == SampleChangerState.Ready

        if offline:
            state_str = "OFFLINE"
        elif self._running or self._sc_state == SampleChangerState.Moving:
            state_str = "MOVING"
        elif self._sc_state == SampleChangerState.Disabled:
            state_str = "DISABLED"
        elif ready:
            state_str = "READY"
        elif self._sc_state in (
            SampleChangerState.Alarm,
            SampleChangerState.Fault,
        ):
            state_str = "OFFLINE"
        else:
            state_str = "UNKNOWN"

        state_dict = {
            "toolopen": self._toolopen,
            "powered": self._powered,
            "running": self._running,
            "regulating": self._regulating,
            "lid1": self._lid1state,
            "lid2": self._lid2state,
            "lid3": self._lid3state,
            "state": state_str,
            "connection": self._connection_state,
        }

        # When offline, disable every action — calling them would raise
        # at the Tango layer anyway and confuse the UI further.
        # Power control must work precisely when the SC is NOT ready: powered
        # off, the Tango State reads DISABLE/OFF -> _sc_state == Disabled ->
        # ready == False. Gating powerOn on `ready` would leave it permanently
        # disabled. So gate power only on connection + the power flag. Explicit
        # `is True/False` so a not-yet-read `_powered is None` disables both
        # rather than falsely enabling powerOn.
        online = not offline
        cmd_state = {
            "powerOn": online and (self._powered is False),
            "powerOff": online and (self._powered is True),
            # LN2 regulation is a toggle, and the two directions are gated on
            # power only — never on `ready`. Regulation is independent of what
            # the arm is doing, and the UI must be able to switch it back off
            # (or on) while a trajectory runs. `is True/False` so an unread
            # `_regulating is None` disables both rather than guessing.
            "regulon": online and self._powered and (self._regulating is False),
            "reguloff": online and self._powered and (self._regulating is True),
            "openlid1": (not self._lid1state) and self._powered and ready,
            "closelid1": self._lid1state and self._powered and ready,
            "openlid2": (not self._lid2state) and self._powered and ready,
            "closelid2": self._lid2state and self._powered and ready,
            "openlid3": (not self._lid3state) and self._powered and ready,
            "closelid3": self._lid3state and self._powered and ready,
            "drysoak": (not self._running) and self._powered and ready,
            "dryht": (not self._running) and self._powered and ready,
            "home": (not self._running) and self._powered and ready,
            "back": (not self._running) and self._powered and ready,
            "safe": (not self._running) and self._powered and ready,
            "clear_memory": not offline,
            "reset": not offline,
            "abort": not offline,
        }

        message = (
            "Sample changer OFFLINE — Tango/PyCATS unreachable"
            if offline
            else self._message
        )
        return state_dict, cmd_state, message

    def re_emit_values(self):
        for channel_name, handler_name in self.CHANNEL_HANDLERS.items():
            channel = getattr(self, channel_name, None)
            handler = getattr(self, handler_name, None)
            if channel is None or handler is None:
                continue
            try:
                handler(channel.get_value())
            except Exception:
                logging.getLogger("HWR").exception(
                    "SOLEILCats: re_emit_values failed for %s", channel_name
                )

    def get_cmd_info(self):
        """Maintenance UI button structure."""
        return [
            [
                "Power",
                [
                    ["powerOn", "PowerOn", "Switch Power On"],
                    ["powerOff", "PowerOff", "Switch Power Off"],
                    # Rendered by the web UI as a single reactive ON/OFF
                    # toggle (see SampleChangerMaintenance.jsx); both entries
                    # must exist so either direction can be dispatched.
                    ["regulon", "Regulation On", "Switch LN2 Regulation On"],
                    ["reguloff", "Regulation Off", "Switch LN2 Regulation Off"],
                ],
            ],
            [
                "Lids",
                [
                    ["openlid1", "Open Lid 1", "Open Lid 1"],
                    ["closelid1", "Close Lid 1", "Close Lid 1"],
                    ["openlid2", "Open Lid 2", "Open Lid 2"],
                    ["closelid2", "Close Lid 2", "Close Lid 2"],
                    ["openlid3", "Open Lid 3", "Open Lid 3"],
                    ["closelid3", "Close Lid 3", "Close Lid 3"],
                ],
            ],
            [
                "Actions",
                [
                    ["home", "Home"],
                    ["drysoak", "Dry and Soak"],
                    ["dryht", "D&S room temp", "Dry and soak at room temperature"],
                ],
            ],
            [
                "Recovery",
                [
                    [
                        "clear_memory",
                        "Clear Memory",
                        "Clear info in robot memory (incl. sample on diffr)",
                    ],
                    ["reset", "Reset Message", "Reset CATS state"],
                    ["back", "Back", "Move sample back into dewar"],
                    ["safe", "Safe", "Move arm to safe position"],
                ],
            ],
            ["Abort", [["abort", "Abort", "Abort execution of command"]]],
        ]

    def _is_device_moving(self):
        """True when the CATS arm is running a path (a load/unload/move in
        progress). Reads the same ``_chnPathRunning`` channel the task loop
        polls, so a command is only sent to the Tango device while it is idle.
        """
        return str(self._chnPathRunning.get_value()).lower() == "true"

    def _execute_server_task(self, method, *args):
        method(*args)
        gevent.sleep(1.0)
        while str(self._chnPathRunning.get_value()).lower() == "true":
            gevent.sleep(0.1)
        return True

    def send_command(self, cmd_name, args=None):
        """Dispatch a UI-named command (powerOn, openlid1, soak, ...) to
        the underlying framework command.
        """
        attr = self.UI_COMMAND_MAP.get(cmd_name)
        if attr is None:
            raise Exception("Unknown sample-changer command: %s" % cmd_name)
        cmd = getattr(self, attr, None)
        if cmd is None:
            raise Exception("Command %s not configured in YAML" % attr)

        tool = self.get_current_tool()

        if cmd_name in ("safe", "home"):
            if tool is None:
                raise Exception(
                    "Cannot detect TOOL type. %s ignored." % cmd_name
                )
            if args is None:
                args = [tool]
        elif cmd_name == "drysoak":
            if tool not in (TOOL_DOUBLE_GRIPPER, TOOL_UNIPUCK):
                raise Exception("Can DRY & SOAK only with UNIPUCK or DOUBLE tool")
            args = [str(tool), str(self.soak_lid)]
        elif cmd_name == "back":
            if tool is None:
                raise Exception("Cannot detect TOOL type. back ignored.")
            args = [tool, 0]

        try:
            if args is not None:
                if isinstance(args, (list, tuple)) and len(args) > 1:
                    return cmd(list(map(str, args)))
                if isinstance(args, (list, tuple)):
                    return cmd(*args)
                return cmd(args)
            return cmd()
        except Exception as exc:
            traceback.print_exc()
            raise Exception(str(exc))
