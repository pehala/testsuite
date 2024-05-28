"""Limitador CR object"""

import dataclasses

from openshift_client import selector

from testsuite.openshift import OpenShiftObject, modify
from testsuite.openshift.authorino import TracingOptions
from testsuite.openshift.deployment import Deployment
from testsuite.utils import asdict


class LimitadorCR(OpenShiftObject):
    """Represents Limitador CR objects"""

    @property
    def deployment(self) -> Deployment:
        """Returns Deployment object for this Limitador"""
        with self.context:
            return selector("deployment/limitador-limitador").object(cls=Deployment)

    def __getitem__(self, name):
        return self.model.spec[name]

    @modify
    def __setitem__(self, name, value):
        if dataclasses.is_dataclass(value):
            self.model.spec[name] = asdict(value)
        else:
            self.model.spec[name] = value
