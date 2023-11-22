"""Contains capability related classes"""
from enum import Flag, auto
from weakget import weakget

from testsuite.config import settings
from testsuite.utils import Singleton


class Capability(Flag):
    """Available capabilities"""

    NONE = 0
    AUTHORINO_STANDALONE = auto()  # Authorino is deployed through authorino-operator
    LIMITADOR_STANDALONE = auto()  # Limitador is deployed through limitador-operator
    KUADRANT_DEPLOYMENT = auto()  # Authorino and Kuadrant are deployed through kuadrant-operator
    MGC = auto()  # Multicluster-gateway-controller is deployed


class CapabilityRegistry(metaclass=Singleton):
    """Registry of gathered capabilities, including reasons why some are missing"""

    def __init__(self):
        super().__init__()
        self._available = Capability.NONE
        self._missing = {}
        self.init()

    def has_kuadrant(self):
        """Returns True, if Kuadrant deployment is present and should be used"""
        spokes = weakget(settings)["control_plane"]["spokes"] % {}

        if not settings.get("gateway_api", True):
            return False, "Gateway API is turned off"

        for name, openshift in spokes.items():
            # Try if Kuadrant is deployed
            if not openshift.connected:
                return False, f"Spoke {name} is not connected"
            project = settings["service_protection"]["system_project"]
            kuadrant_openshift = openshift.change_project(project)
            kuadrants = kuadrant_openshift.do_action("get", "kuadrant", "-o", "json", parse_output=True)
            if len(kuadrants.model["items"]) == 0:
                return False, f"Spoke {name} does not have Kuadrant resource in project {project}"

        return True, ""

    def has_mgc(self):
        """Returns True, if MGC is configured and deployed"""
        spokes = weakget(settings)["control_plane"]["spokes"] % {}

        if not settings.get("gateway_api", True):
            return False, "Gateway API is turned off"

        if len(spokes) == 0:
            return False, "Spokes are not configured"

        hub_openshift = settings["control_plane"]["hub"]
        if not hub_openshift.connected:
            return False, "Control Plane Hub Openshift is not connected"

        if "managedzones" not in hub_openshift.do_action("api-resources", "--api-group=kuadrant.io").out():
            return False, "MGC custom resources are missing on hub cluster"
        return True, ""

    def init(self):
        """Gathers data and decides what capabilities are present"""
        kuadrant, kuadrant_reason = self.has_kuadrant()
        mgc, mgc_reason = self.has_mgc()

        if kuadrant:
            self._available |= Capability.KUADRANT_DEPLOYMENT
            self._missing[Capability.AUTHORINO_STANDALONE] = "Kuadrant deployment is configured and enabled"
            self._missing[Capability.LIMITADOR_STANDALONE] = "Kuadrant deployment is configured and enabled"
        else:
            self._available |= Capability.AUTHORINO_STANDALONE | Capability.LIMITADOR_STANDALONE
            self._missing[Capability.KUADRANT_DEPLOYMENT] = kuadrant_reason

        if mgc:
            self._available |= Capability.MGC
        else:
            self._missing[Capability.MGC] = mgc_reason

    @property
    def available(self) -> Capability:
        """Returns all available Capabilities"""
        return self._available

    @property
    def missing(self) -> dict[Capability, str]:
        """Returns dict of all missing capabilities and their respective reason for why they are not present"""
        return self._missing
