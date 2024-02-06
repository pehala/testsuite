"""Root conftest"""

import signal
from urllib.parse import urlparse

import pytest
from dynaconf import ValidationError
from keycloak import KeycloakAuthenticationError

from testsuite.capabilities import has_kuadrant, has_mgc, is_standalone
from testsuite.certificates import CFSSLClient
from testsuite.config import settings
from testsuite.mockserver import Mockserver
from testsuite.gateway import Gateway, GatewayRoute, Hostname, Exposer
from testsuite.oidc import OIDCProvider
from testsuite.oidc.auth0 import Auth0Provider
from testsuite.openshift.httpbin import Httpbin
from testsuite.oidc.rhsso import RHSSO
from testsuite.gateway.envoy import Envoy
from testsuite.gateway.envoy.route import EnvoyVirtualRoute
from testsuite.gateway.gateway_api.gateway import KuadrantGateway
from testsuite.gateway.gateway_api.hostname import OpenShiftExposer
from testsuite.gateway.gateway_api.route import HTTPRoute
from testsuite.utils import randomize, _whoami


def pytest_addoption(parser):
    """Add options to include various kinds of tests in testrun"""
    parser.addoption(
        "--performance", action="store_true", default=False, help="Run also performance tests (default: False)"
    )
    parser.addoption("--enforce", action="store_true", default=False, help="Fails tests instead of skip")


def pytest_runtest_setup(item):
    """Exclude performance tests by default, require explicit option"""
    marks = [i.name for i in item.iter_markers()]
    if "performance" in marks and not item.config.getoption("--performance"):
        pytest.skip("Excluding performance tests")
    skip_func = pytest.fail if item.config.getoption("--enforce") else pytest.skip
    if "kuadrant_only" in marks:
        kuadrant, error = has_kuadrant()
        if not kuadrant:
            skip_func(f"Unable to locate Kuadrant installation: {error}")
    if "standalone_only" in marks:
        status, error = is_standalone()
        if not status:
            skip_func(f"Unable to run Standalone tests: {error}")
    if "mgc" in marks:
        mgc, error = has_mgc()
        if not mgc:
            skip_func(f"Unable to locate MGC installation: {error}")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):  # pylint: disable=unused-argument
    """Add jira link to html report"""
    pytest_html = item.config.pluginmanager.getplugin("html")
    outcome = yield
    report = outcome.get_result()
    extra = getattr(report, "extra", [])
    if report.when == "setup":
        for marker in item.iter_markers(name="issue"):
            issue = marker.args[0]
            url = urlparse(issue)
            if "github" in url.hostname:
                label = url.path.replace("/issues/", "#")[1:]
            else:
                label = issue
            extra.append(pytest_html.extras.url(issue, name=label))
        report.extra = extra


@pytest.fixture(scope="session", autouse=True)
def term_handler():
    """
    This will handle ^C, cleanup won't be skipped
    https://github.com/pytest-dev/pytest/issues/9142
    """
    orig = signal.signal(signal.SIGTERM, signal.getsignal(signal.SIGINT))
    yield
    signal.signal(signal.SIGTERM, orig)


# pylint: disable=unused-argument
def pytest_collection_modifyitems(session, config, items):
    """
    Add user properties to testcases for xml output

    This adds issue and issue-id properties to junit output, utilizes
    pytest.mark.issue marker.

    This is copied from pytest examples to record custom properties in junit
    https://docs.pytest.org/en/stable/usage.html
    """

    for item in items:
        for marker in item.iter_markers(name="issue"):
            issue = marker.args[0]
            item.user_properties.append(("issue", issue))


@pytest.fixture(scope="session")
def testconfig():
    """Testsuite settings"""
    return settings


@pytest.fixture(scope="module")
def openshift(testconfig):
    """OpenShift client for the primary namespace"""
    client = testconfig["service_protection"]["project"]
    if not client.connected:
        pytest.fail("You are not logged into Openshift or the namespace doesn't exist")
    return client


@pytest.fixture(scope="module")
def openshift2(testconfig):
    """OpenShift client for the secondary namespace located on the same cluster as primary Openshift"""
    client = testconfig["service_protection"]["project2"]
    if client is None:
        pytest.skip("Openshift2 required but second_project was not set")
    if not client.connected:
        pytest.fail("You are not logged into Openshift or the namespace for Openshift2 doesn't exist")
    return client


@pytest.fixture(scope="session")
def rhsso(request, testconfig, blame):
    """RHSSO OIDC Provider fixture"""
    try:
        testconfig.validators.validate(only="rhsso")
        cnf = testconfig["rhsso"]
        info = RHSSO(
            cnf["url"],
            cnf["username"],
            cnf["password"],
            blame("realm"),
            blame("client"),
            cnf["test_user"]["username"],
            cnf["test_user"]["password"],
        )

        if not testconfig["skip_cleanup"]:
            request.addfinalizer(info.delete)

        info.commit()
        return info
    except KeycloakAuthenticationError:
        return pytest.skip("Unable to login into SSO, please check the credentials provided")
    except KeyError as exc:
        return pytest.skip(f"SSO configuration item is missing: {exc}")


@pytest.fixture(scope="session")
def auth0(testconfig):
    """Auth0 OIDC provider fixture"""
    try:
        section = testconfig["auth0"]
        return Auth0Provider(section["url"], section["client_id"], section["client_secret"])
    except KeyError as exc:
        return pytest.skip(f"Auth0 configuration item is missing: {exc}")


@pytest.fixture(scope="session")
def cfssl(testconfig):
    """CFSSL client library"""
    client = CFSSLClient(binary=testconfig["cfssl"])
    if not client.exists:
        pytest.skip("Skipping CFSSL tests as CFSSL binary path is not properly configured")
    return client


@pytest.fixture(scope="module")
def mockserver(testconfig):
    """Returns mockserver"""
    try:
        testconfig.validators.validate(only=["mockserver"])
        return Mockserver(testconfig["mockserver"]["url"])
    except (KeyError, ValidationError) as exc:
        return pytest.skip(f"Mockserver configuration item is missing: {exc}")


@pytest.fixture(scope="session")
def oidc_provider(rhsso) -> OIDCProvider:
    """Fixture which enables switching out OIDC providers for individual modules"""
    return rhsso


@pytest.fixture(scope="session")
def blame(request):
    """Returns function that will add random identifier to the name"""

    def _blame(name: str, tail: int = 3) -> str:
        """Create 'scoped' name within given test

        This returns unique name for object(s) to avoid conflicts

        Args:
            :param name: Base name, e.g. 'svc'
            :param tail: length of random suffix"""

        nodename = request.node.name
        if nodename.startswith("test_"):  # is this always true?
            nodename = nodename[5:]

        context = nodename.lower().split("_")[0]
        if len(context) > 2:
            context = context[:2] + context[2:-1].translate(str.maketrans("", "", "aiyu")) + context[-1]

        if "." in context:
            context = context.split(".")[0]

        return randomize(f"{name[:8]}-{_whoami()[:8]}-{context[:9]}", tail=tail)

    return _blame


@pytest.fixture(scope="session")
def label(blame):
    """Session scope label for all resources"""
    return blame("testrun")


@pytest.fixture(scope="module")
def module_label(label):
    """Module scope label for all resources"""
    return randomize(label)


@pytest.fixture(scope="module")
def kuadrant(testconfig, openshift):
    """Returns Kuadrant instance if exists, or None"""
    if not testconfig.get("standalone", False):
        return None

    # Try if Kuadrant is deployed
    kuadrant_openshift = testconfig["service_protection"]["system_project"]
    kuadrants = kuadrant_openshift.do_action("get", "kuadrant", "-o", "json", parse_output=True)
    if len(kuadrants.model["items"]) == 0:
        pytest.fail("Running Kuadrant tests, but Kuadrant resource was not found")

    # TODO: Return actual Kuadrant object
    return True


@pytest.fixture(scope="module")
def backend(request, openshift, blame, label):
    """Deploys Httpbin backend"""
    httpbin = Httpbin(openshift, blame("httpbin"), label)
    request.addfinalizer(httpbin.delete)
    httpbin.commit()
    return httpbin


@pytest.fixture(scope="module")
def gateway(request, kuadrant, openshift, blame, backend, module_label, testconfig, wildcard_domain) -> Gateway:
    """Deploys Envoy that wire up the Backend behind the reverse-proxy and Authorino instance"""
    if kuadrant:
        gw = KuadrantGateway.create_instance(openshift, blame("gw"), wildcard_domain, {"app": module_label})
    else:
        authorino = request.getfixturevalue("authorino")
        gw = Envoy(
            openshift,
            blame("gw"),
            authorino,
            testconfig["service_protection"]["envoy"]["image"],
            labels={"app": module_label},
        )
    request.addfinalizer(gw.delete)
    gw.commit()
    return gw


@pytest.fixture(scope="module")
def route(request, kuadrant, gateway, blame, hostname, backend, module_label) -> GatewayRoute:
    """Route object"""
    if kuadrant:
        route = HTTPRoute.create_instance(gateway.openshift, blame("route"), gateway, {"app": module_label})
    else:
        route = EnvoyVirtualRoute.create_instance(gateway.openshift, blame("route"), gateway)
    route.add_hostname(hostname.hostname)
    route.add_backend(backend)
    request.addfinalizer(route.delete)
    route.commit()
    return route


@pytest.fixture(scope="module")
def exposer(request) -> Exposer:
    """Exposer object instance"""
    exposer = OpenShiftExposer()
    request.addfinalizer(exposer.delete)
    exposer.commit()
    return exposer


@pytest.fixture(scope="module")
def hostname(gateway, exposer, blame) -> Hostname:
    """Exposed Hostname object"""
    hostname = exposer.expose_hostname(blame("hostname"), gateway)
    return hostname


@pytest.fixture(scope="module")
def wildcard_domain(openshift):
    """
    Wildcard domain of openshift cluster
    """
    return f"*.{openshift.apps_url}"


@pytest.fixture(scope="module")
def client(route, hostname):
    """Returns httpx client to be used for requests, it also commits AuthConfig"""
    client = hostname.client()
    yield client
    client.close()
