from collections import namedtuple
import logging
import platform
import subprocess

from middlewared.utils import run

logger = logging.getLogger(__name__)

IS_LINUX = platform.system() == "Linux"

ServiceState = namedtuple("ServiceState", ["running", "pids"])


class ServiceInterface:
    name = NotImplemented

    etc = []
    restartable = False  # Implements `restart` method instead of `stop` + `start`
    reloadable = False  # Implements `reload` method

    def __init__(self, middleware):
        self.middleware = middleware

    async def get_state(self):
        raise NotImplementedError

    async def start(self):
        raise NotImplementedError

    async def before_start(self):
        pass

    async def after_start(self):
        pass

    async def stop(self):
        raise NotImplementedError

    async def before_stop(self):
        pass

    async def after_stop(self):
        pass

    async def restart(self):
        raise NotImplementedError

    async def before_restart(self):
        pass

    async def after_restart(self):
        pass

    async def reload(self):
        raise NotImplementedError

    async def before_reload(self):
        pass

    async def after_reload(self):
        pass


class SimpleServiceLinux:
    async def _get_state_linux(self):
        pass

    async def _start_linux(self):
        pass

    async def _stop_linux(self):
        pass

    async def _restart_linux(self):
        raise NotImplementedError

    async def _reload_linux(self):
        pass


class SimpleServiceFreeBSD:
    freebsd_rc = NotImplemented
    freebsd_pidfile = None
    freebsd_procname = None

    async def _get_state_freebsd(self):
        procname = self.freebsd_procname or self.freebsd_rc

        if self.freebsd_pidfile:
            cmd = ["pgrep", "-F", self.freebsd_pidfile, procname]
        else:
            cmd = ["pgrep", procname]

        proc = await run(*cmd, check=False)
        if proc.returncode == 0:
            return ServiceState(True, [
                int(i)
                for i in proc.stdout.strip().split('\n') if i.isdigit()
            ])
        else:
            return ServiceState(False, [])

    async def _start_freebsd(self):
        await self._freebsd_service(self.freebsd_rc, "restart")

    async def _stop_freebsd(self):
        await self._freebsd_service(self.freebsd_rc, "stop", force=True)

    async def _restart_freebsd(self):
        raise NotImplementedError

    async def _reload_freebsd(self):
        await self._freebsd_service(self.freebsd_rc, "reload")

    async def _freebsd_service(self, rc, verb, force=False):
        if force:
            preverb = "force"
        else:
            preverb = "one"

        result = await run("service", rc, preverb + verb, check=False, encoding="utf-8", stderr=subprocess.STDOUT)
        if verb != "status" and result.returncode != 0:
            logger.warning("%s %s failed with code %d: %r", rc, preverb + verb, result.returncode, result.stdout)

        return result


class SimpleService(ServiceInterface, SimpleServiceLinux, SimpleServiceFreeBSD):
    async def get_state(self):
        if IS_LINUX:
            return await self._get_state_linux()
        else:
            return await self._get_state_freebsd()

    async def start(self):
        if IS_LINUX:
            return await self._start_linux()
        else:
            return await self._start_freebsd()

    async def stop(self):
        if IS_LINUX:
            return await self._stop_linux()
        else:
            return await self._stop_freebsd()

    async def restart(self):
        if IS_LINUX:
            return await self._restart_linux()
        else:
            return await self._restart_freebsd()

    async def reload(self):
        if IS_LINUX:
            return await self._reload_linux()
        else:
            return await self._reload_freebsd()
