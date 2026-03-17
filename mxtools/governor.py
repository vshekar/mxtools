"""
Rationale:

The Governor is a dynamic beast. The PVs it exposes depend
on the configuration files given to it. Therefore, it would be
hopelessly tedious to manually keep a Governor ophyd object in
sync with the Governor IOC. There are at least two solutions to
this issue:

1. Have the IOC auto-generate Governor ohpyd objects

This would require the IOC to produce a Python file (perhaps via
Jinja) and then to, somehow, synchronize this file with the rest
of the startup files for the beamline.

2. Have the ophyd object be dynamically generated at startup time

No modifications to the IOC would be required, as long as the IOC
exports enough metadata. The downside of this approach is that
there is some encapsulation leakage (direct cagets) and there's
a risk that the Governor IOC won't be up when this function runs.

We take approach #2 here, with the hope that the benefits will
outweight the drawbacks.

NOTE: given that the Governor ophyd object is created dynamically,
the Governor IOC *must be running* when this file runs.

The overall available auto-generated API will be as follows
(all leafs are EpicsSignals):

govs = _make_governors('XF:19IDC-ES', name='govs')

#
# Global Governor control
#

# Controls whether any Governor is active or not:
govs.sel.active

# Selects which Governor to use ("Human", "Robot"):
govs.sel.config

# Alias for the Robot configuration
gov_rbt = govs.gov.Robot

#
# Meta-data
#

# Current state
gov_rbt.state

# All existing states
gov_rbt.states

# All existing devices
gov_rbt.devices

# All currently reachable states
gov_rbt.reachable

# All targets of the device "bsy"
gov_rbt.dev.bsy.targets

#
# Per-device configuration
#

# Position for target "Down" of device "bsy"
gov_rbt.dev.bsy.target_Down

# Low limit of device "bsy" when at state "SE"
gov_rbt.dev.bsy.at_SE.low

# Pos for high limit of device "bsy" when at state "SE":
gov_rbt.dev.bsy.at_SE.high

#
# Changing state
#

# Attempt to move the Governor to the SE state
# (behaves as a positioner)
RE(bps.abs_set(gov_rbt, 'SE', wait=True))
"""

from typing import Dict, List

from ophyd import Component as Cpt
from ophyd import Device
from ophyd import DynamicDeviceComponent as DDCpt
from ophyd import EpicsSignal, EpicsSignalRO, PVPositionerPC, get_cl


class GovernorPositioner(PVPositionerPC):
    """Mixin to control the Governor state as a positioner"""

    setpoint = Cpt(EpicsSignal, "}Cmd:Go-Cmd")
    readback = Cpt(EpicsSignalRO, "}Sts:State-I")
    done = Cpt(EpicsSignalRO, "}Sts:Busy-Sts")
    done_value = 0


class GovernorMeta(Device):
    """Mixin to expose metadata for the Governor"""

    # Metadata

    # Current state: str
    state = Cpt(EpicsSignalRO, "}Sts:State-I")

    # All available states: List[str]
    states = Cpt(EpicsSignalRO, "}Sts:States-I")

    # States that are reachable from current state: List[str]
    reachable = Cpt(EpicsSignalRO, "}Sts:Reach-I")

    # All existing "devices": List[str]
    devices = Cpt(EpicsSignalRO, "}Sts:Devs-I")


class GovernorDriver(Device):
    # Active: enum ["Inactive", "Active"]
    # controls whether any Governor can make any changes.
    # When moving the robot, it is useful to set
    # active = "Inactive" beforehand to prevent the
    # goniometer from moving and causing a crash.
    active = Cpt(EpicsSignal, "Active-Sel")

    # Config: enum with available governors
    # (typically ["Human", "Robot"], but depends on the
    # configuration file)
    # Select which Governor to use.
    config = Cpt(EpicsSignal, "Config-Sel", string=True)


class GovernorDeviceLimits(Device):
    low = Cpt(EpicsSignal, "LLim-Pos")
    high = Cpt(EpicsSignal, "HLim-Pos")


def _make_governor_device(targets: List[str], states: List[str]) -> type:
    """Returns a dynamically created class that represents a
    Governor device, with its existing targets and limits."""
    targets_attr = [("targets", Cpt(EpicsSignal, "Sts:Tgts-I"))]

    # Targets of a device. A target is a named position.
    # Example PV: XF:19IDC-ES{Gov:Robot-Dev:cxy}Pos:Near-Pos
    # Target named "Near" for the cxy device.
    target_attrs = [(f"target_{target}", Cpt(EpicsSignal, f"Pos:{target}-Pos")) for target in targets]

    # Limits of a device for each state.
    # Example PVs: XF:19IDC-ES{Gov:Robot-Dev:cxy}SA:LLim-Pos
    #              XF:19IDC-ES{Gov:Robot-Dev:cxy}SA:HLim-Pos
    # Low and High limits for the cxy device at state SA
    limit_attrs = [(f"at_{state}", Cpt(GovernorDeviceLimits, f"{state}:")) for state in states]

    return type("GovernorDevice", (Device,), dict(targets_attr + target_attrs + limit_attrs))


def _make_governor(prefix: str) -> type:
    """Returns a dynamically created class that represents a
    single Governor configuration (example: "Robot")
    """
    cl = get_cl()

    # Fetch all Governor device names
    devices: List[str] = cl.caget(f"{prefix}}}Sts:Devs-I")

    # Fetch all Governor state names
    states: List[str] = cl.caget(f"{prefix}}}Sts:States-I")

    # Fetch all existing target names for each device
    device_targets: Dict[str, List[str]] = {
        device: cl.caget(f"{prefix}-Dev:{device}}}Sts:Tgts-I") for device in devices
    }

    class Governor(GovernorPositioner, GovernorMeta):
        dev = DDCpt(
            {
                device: (
                    _make_governor_device(targets, states),
                    f"-Dev:{device}}}",
                    dict(),
                )
                for device, targets in device_targets.items()
            }
        )

    return Governor


def _make_governors(prefix: str, name: str) -> "Governors":  # noqa: F821
    """Returns a dynamically created object that represents
    all available Governors, and allows switching between
    them, as well as deactivating them.
    """
    cl = get_cl()

    gov_names: List[str] = cl.caget(f"{prefix}{{Gov}}Sts:Configs-I")
    # If there is only one Governor, cl.caget will return str
    # instead of a list with a single str
    if isinstance(gov_names, str):
        gov_names = [gov_names]

    try:
        gov_prefixes: List[str] = [f"{prefix}{{Gov:{name}" for name in gov_names]
    except:  # noqa: E722
        # Iteration failed, likely there is no Governor available
        gov_names = []
        gov_prefixes = []

    class Governors(Device):
        sel = Cpt(GovernorDriver, f"{prefix}{{Gov}}")
        gov = DDCpt(
            {
                gov_name: (_make_governor(gov_prefix), gov_prefix, dict())
                for gov_name, gov_prefix in zip(gov_names, gov_prefixes)
            }
        )

    return Governors("", name=name)
