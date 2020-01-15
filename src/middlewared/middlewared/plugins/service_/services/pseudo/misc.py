import platform

from middlewared.plugins.service_.services.base import ServiceInterface, SimpleService

from middlewared.utils import run


class HostnameService(ServiceInterface):
    name = "hostname"

    async def reload(self):
        await run(["hostname", ""], check=False)
        await self.middleware.call("etc.generate", "hostname")
        await self.middleware.call("etc.generate", "rc")
        if platform.system() == "FreeBSD":
            await run(["service", "hostname", "start"])
        await self.middleware.call("service.reload", "mdns")
        await self.middleware.call("service.restart", "collectd")


class NetworkService(ServiceInterface):
    name = "network"

    async def start(self):
        await self.middleware.call("interface.sync")
        await self.middleware.call("route.sync")


class NetworkGeneral(ServiceInterface):
    name = "networkgeneral"

    async def reload(self):
        await self.middleware.call("service.reload", "resolvconf")
        if platform.system() == "FreeBSD":
            await run(["service", "routing", "restart"])


class PowerDService(SimpleService):
    name = "powerd"

    freebsd_rc = "powerd"


class RCService(ServiceInterface):
    name = "rc"

    reloadable = True

    async def reload(self):
        await self.middleware.call("etc.generate", "rc")


class ResolvConfService(ServiceInterface):
    name = "resolvconf"

    async def reload(self):
        await self.middleware.call("service.reload", "hostname")
        await self.middleware.call("dns.sync")


class RoutingService(SimpleService):
    name = "routing"

    freebsd_rc = "routing"




class SysconsService(SimpleService):
    name = "syscons"

    restartable = True

    freebsd_rc = "syscons"


class SysctlService(ServiceInterface):
    name = "sysctl"

    reloadable = True

    async def reload(self):
        await self.middleware.call("etc.generate", "sysctl")
