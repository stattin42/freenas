import asyncio
import contextlib
import errno
import inspect
import os
import psutil
import signal
try:
    import sysctl
except ImportError:
    sysctl = None
import threading
import time
import subprocess

from middlewared.schema import accepts, Bool, Dict, Int, Ref, Str
from middlewared.service import filterable, CallError, CRUDService, private
import middlewared.sqlalchemy as sa
from middlewared.utils import Popen, filter_list, run
from middlewared.utils.contextlib import asyncnullcontext


class ServiceDefinition:
    def __init__(self, *args):
        if len(args) == 2:
            self.procname = args[0]
            self.rc_script = args[0]
            self.pidfile = args[1]

        elif len(args) == 3:
            self.procname = args[0]
            self.rc_script = args[1]
            self.pidfile = args[2]

        else:
            raise ValueError("Invalid number of arguments passed (must be 2 or 3)")


class StartNotify(threading.Thread):

    def __init__(self, pidfile, verb, *args, **kwargs):
        self._pidfile = pidfile
        self._verb = verb

        if self._pidfile:
            try:
                with open(self._pidfile) as f:
                    self._pid = f.read()
            except IOError:
                self._pid = None

        super(StartNotify, self).__init__(*args, **kwargs)

    def run(self):
        """
        If we are using start or restart we expect that a .pid file will
        exists at the end of the process, so we wait for said pid file to
        be created and check if its contents are non-zero.
        Otherwise we will be stopping and expect the .pid to be deleted,
        so wait for it to be removed
        """
        if not self._pidfile:
            return None

        tries = 1
        while tries < 6:
            time.sleep(1)
            if self._verb in ('start', 'restart'):
                if os.path.exists(self._pidfile):
                    # The file might have been created but it may take a
                    # little bit for the daemon to write the PID
                    time.sleep(0.1)
                try:
                    with open(self._pidfile) as f:
                        pid = f.read()
                except IOError:
                    pid = None

                if pid:
                    if self._verb == 'start':
                        break
                    if self._verb == 'restart':
                        if pid != self._pid:
                            break
                        # Otherwise, service has not restarted yet
            elif self._verb == "stop" and not os.path.exists(self._pidfile):
                break
            tries += 1


class ServiceModel(sa.Model):
    __tablename__ = 'services_services'

    id = sa.Column(sa.Integer(), primary_key=True)
    srv_service = sa.Column(sa.String(120))
    srv_enable = sa.Column(sa.Boolean(), default=False)


class ServiceService(CRUDService):

    @filterable
    async def query(self, filters=None, options=None):
        """
        Query all system services with `query-filters` and `query-options`.
        """
        if options is None:
            options = {}
        options['prefix'] = 'srv_'

        services = await self.middleware.call('datastore.query', 'services.services', filters, options)

        # In case a single service has been requested
        if not isinstance(services, list):
            services = [services]

        jobs = {
            asyncio.ensure_future(self._get_status(entry)): entry
            for entry in services
        }
        if jobs:
            done, pending = await asyncio.wait(list(jobs.keys()), timeout=15)

        def result(task):
            """
            Method to handle results of the coroutines.
            In case of error or timeout, provide UNKNOWN state.
            """
            result = None
            try:
                if task in done:
                    result = task.result()
            except Exception:
                pass
            if result is None:
                entry = jobs.get(task)
                self.logger.warn('Failed to get status for %s', entry['service'])
                entry['state'] = 'UNKNOWN'
                entry['pids'] = []
                return entry
            else:
                return result

        services = list(map(result, jobs))
        return filter_list(services, filters, options)

    @accepts(
        Str('id_or_name'),
        Dict(
            'service-update',
            Bool('enable', default=False),
        ),
    )
    async def do_update(self, id_or_name, data):
        """
        Update service entry of `id_or_name`.

        Currently it only accepts `enable` option which means whether the
        service should start on boot.

        """
        if not id_or_name.isdigit():
            svc = await self.middleware.call('datastore.query', 'services.services', [('srv_service', '=', id_or_name)])
            if not svc:
                raise CallError(f'Service {id_or_name} not found.', errno.ENOENT)
            id_or_name = svc[0]['id']

        rv = await self.middleware.call('datastore.update', 'services.services', id_or_name, {'srv_enable': data['enable']})
        await self.middleware.call('etc.generate', 'rc')
        return rv

    @accepts(
        Str('service'),
        Dict(
            'service-control',
            Bool('onetime', default=True),
            Bool('wait', default=None, null=True),
            Bool('sync', default=None, null=True),
            register=True,
        ),
    )
    async def start(self, service, options=None):
        """ Start the service specified by `service`.

        The helper will use method self._start_[service]() to start the service.
        If the method does not exist, it would fallback using service(8)."""
        await self.middleware.call_hook('service.pre_action', service, 'start', options)
        sn = self._started_notify("start", service)
        await self._simplecmd("start", service, options)
        return await self.started(service, sn)

    async def started(self, service, sn=None):
        """
        Test if service specified by `service` has been started.
        """
        if sn:
            await self.middleware.run_in_thread(sn.join)

        try:
            svc = await self.query([('service', '=', service)], {'get': True})
            self.middleware.send_event('service.query', 'CHANGED', fields=svc)
            return svc['state'] == 'RUNNING'
        except IndexError:
            f = getattr(self, '_started_' + service, None)
            if callable(f):
                if inspect.iscoroutinefunction(f):
                    return (await f())[0]
                else:
                    return f()[0]
            else:
                return (await self._started(service))[0]

    @accepts(
        Str('service'),
        Ref('service-control'),
    )
    async def stop(self, service, options=None):
        """ Stop the service specified by `service`.

        The helper will use method self._stop_[service]() to stop the service.
        If the method does not exist, it would fallback using service(8)."""
        await self.middleware.call_hook('service.pre_action', service, 'stop', options)
        sn = self._started_notify("stop", service)
        await self._simplecmd("stop", service, options)
        return await self.started(service, sn)

    @accepts(
        Str('service'),
        Ref('service-control'),
    )
    async def restart(self, service, options=None):
        """
        Restart the service specified by `service`.

        The helper will use method self._restart_[service]() to restart the service.
        If the method does not exist, it would fallback using service(8)."""
        await self.middleware.call_hook('service.pre_action', service, 'restart', options)
        sn = self._started_notify("restart", service)
        await self._simplecmd("restart", service, options)
        return await self.started(service, sn)

    @accepts(
        Str('service'),
        Ref('service-control'),
    )
    async def reload(self, service, options=None):
        """
        Reload the service specified by `service`.

        The helper will use method self._reload_[service]() to reload the service.
        If the method does not exist, the helper will try self.restart of the
        service instead."""
        await self.middleware.call_hook('service.pre_action', service, 'reload', options)
        try:
            await self._simplecmd("reload", service, options)
        except Exception as e:
            await self.restart(service, options)
        return await self.started(service)

    async def _get_status(self, service):
        f = getattr(self, '_started_' + service['service'], None)
        if callable(f):
            if inspect.iscoroutinefunction(f):
                running, pids = await f()
            else:
                running, pids = f()
        else:
            running, pids = await self._started(service['service'])

        if running:
            state = 'RUNNING'
        else:
            state = 'STOPPED'

        service['state'] = state
        service['pids'] = pids
        return service

    async def _simplecmd(self, action, what, options=None):
        self.logger.debug("Calling: %s(%s) ", action, what)
        f = getattr(self, '_' + action + '_' + what, None)
        if f is None:
            # Provide generic start/stop/restart verbs for rc.d scripts
            if what in self.SERVICE_DEFS:
                if self.SERVICE_DEFS[what].rc_script:
                    what = self.SERVICE_DEFS[what].rc_script
            if action in ("start", "stop", "restart", "reload"):
                if action == 'restart':
                    await self._system("/usr/sbin/service " + what + " forcestop ")
                await self._service(what, action, **options)
            else:
                raise ValueError("Internal error: Unknown command")
        else:
            call = f(**(options or {}))
            if inspect.iscoroutinefunction(f):
                await call

    async def _system(self, cmd):
        proc = await Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=True, close_fds=True)
        stdout = (await proc.communicate())[0]
        if proc.returncode != 0 and "status" not in cmd:
            self.logger.warning("Command %r failed with code %d: %r", cmd, proc.returncode, stdout)
        return proc.returncode

    async def _service(self, service, verb, **options):
        onetime = options.pop('onetime', None)
        force = options.pop('force', None)
        quiet = options.pop('quiet', None)
        extra = options.pop('extra', '')

        # force comes before one which comes before quiet
        # they are mutually exclusive
        preverb = ''
        if force:
            preverb = 'force'
        elif onetime:
            preverb = 'one'
        elif quiet:
            preverb = 'quiet'

        return await self._system('/usr/sbin/service {} {}{} {}'.format(
            service,
            preverb,
            verb,
            extra,
        ))

    def _started_notify(self, verb, what):
        """
        The check for started [or not] processes is currently done in 2 steps
        This is the first step which involves a thread StartNotify that watch for event
        before actually start/stop rc.d scripts

        Returns:
            StartNotify object if the service is known or None otherwise
        """

        if what in self.SERVICE_DEFS:
            sn = StartNotify(verb=verb, pidfile=self.SERVICE_DEFS[what].pidfile)
            sn.start()
            return sn
        else:
            return None

    async def _started(self, what, notify=None):
        """
        This is the second step::
        Wait for the StartNotify thread to finish and then check for the
        status of pidfile/procname using pgrep

        Returns:
            True whether the service is alive, False otherwise
        """

        if what in self.SERVICE_DEFS:
            if notify:
                await self.middleware.run_in_thread(notify.join)

            """
            if self.SERVICE_DEFS[what].pidfile:
                pgrep = "/bin/pgrep -F {}{}".format(
                    self.SERVICE_DEFS[what].pidfile,
                    ' ' + self.SERVICE_DEFS[what].procname if self.SERVICE_DEFS[what].procname else '',
                )
            else:
                pgrep = "/bin/pgrep {}".format(self.SERVICE_DEFS[what].procname)
            proc = await Popen(pgrep, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
            data = (await proc.communicate())[0].decode()

            if proc.returncode == 0:
                return True, [
                    int(i)
                    for i in data.strip().split('\n') if i.isdigit()
                ]
            """
        return False, []

    async def _start_routing(self, **kwargs):
        await self.middleware.call('etc.generate', 'rc')
        await self._service('routing', 'start', **kwargs)

    async def _reload_timeservices(self, **kwargs):
        await self.middleware.call('etc.generate', 'localtime')
        await self.middleware.call('etc.generate', 'ntpd')
        await self._service("ntpd", "restart", **kwargs)
        settings = await self.middleware.call(
            'datastore.query',
            'system.settings',
            [],
            {'order_by': ['-id'], 'get': True}
        )
        os.environ['TZ'] = settings['stg_timezone']
        time.tzset()

    async def _restart_ntpd(self, **kwargs):
        await self.middleware.call('etc.generate', 'ntpd')
        await self._service('ntpd', 'restart', **kwargs)

    async def _start_ssl(self, **kwargs):
        await self.middleware.call('etc.generate', 'ssl')

    async def _start_kmip(self, **kwargs):
        await self._start_ssl(**kwargs)
        await self.middleware.call('etc.generate', 'kmip')

    async def _started_nis(self, **kwargs):
        return (await self.middleware.call('nis.started')), []

    async def _start_nis(self, **kwargs):
        return (await self.middleware.call('nis.start')), []

    async def _restart_nis(self, **kwargs):
        await self.middleware.call('nis.stop')
        return (await self.middleware.call('nis.start')), []

    async def _stop_nis(self, **kwargs):
        return (await self.middleware.call('nis.stop')), []

    async def _started_ldap(self, **kwargs):
        return await self.middleware.call('ldap.started'), []

    async def _start_ldap(self, **kwargs):
        return await self.middleware.call('ldap.start'), []

    async def _stop_ldap(self, **kwargs):
        return await self.middleware.call('ldap.stop'), []

    async def _restart_ldap(self, **kwargs):
        await self.middleware.call('ldap.stop')
        return await self.middleware.call('ldap.start'), []

    async def _started_activedirectory(self, **kwargs):
        return await self.middleware.call('activedirectory.started'), []

    async def _start_activedirectory(self, **kwargs):
        return await self.middleware.call('activedirectory.start'), []

    async def _stop_activedirectory(self, **kwargs):
        return await self.middleware.call('activedirectory.stop'), []

    async def _restart_activedirectory(self, **kwargs):
        await self.middleware.call('kerberos.stop'), []
        return await self.middleware.call('activedirectory.start'), []

    async def _reload_activedirectory(self, **kwargs):
        await self._service("winbindd", "reload", quiet=True, **kwargs)

    async def _restart_syslogd(self, **kwargs):
        await self.middleware.call("etc.generate", "syslogd")
        await self._system("/etc/local/rc.d/syslog-ng restart")

    async def _start_syslogd(self, **kwargs):
        await self.middleware.call("etc.generate", "syslogd")
        await self._system("/etc/local/rc.d/syslog-ng start")

    async def _stop_syslogd(self, **kwargs):
        await self._system("/etc/local/rc.d/syslog-ng stop")

    async def _reload_syslogd(self, **kwargs):
        await self.middleware.call("etc.generate", "syslogd")
        await self._system("/etc/local/rc.d/syslog-ng reload")

    async def _restart_cron(self, **kwargs):
        await self.middleware.call('etc.generate', 'cron')

    async def _start_motd(self, **kwargs):
        await self.middleware.call('etc.generate', 'motd')
        await self._service("motd", "start", quiet=True, **kwargs)

    async def _start_ttys(self, **kwargs):
        await self.middleware.call('etc.generate', 'ttys')

    async def _started_ups(self, **kwargs):
        return await self._started('upsmon')

    async def _restart_system(self, **kwargs):
        asyncio.ensure_future(self.middleware.call('system.reboot', {'delay': 3}))

    async def _stop_system(self, **kwargs):
        asyncio.ensure_future(self.middleware.call('system.shutdown', {'delay': 3}))

    async def _restart_http(self, **kwargs):
        await self.middleware.call("etc.generate", "nginx")
        await self.reload("mdns", kwargs)
        await self._service("nginx", "restart", **kwargs)

    async def _reload_http(self, **kwargs):
        await self.middleware.call("etc.generate", "nginx")
        await self.reload("mdns", kwargs)
        await self._service("nginx", "reload", **kwargs)

    async def _reload_loader(self, **kwargs):
        await self.middleware.call("etc.generate", "loader")

    async def _restart_disk(self, **kwargs):
        await self._reload_disk(**kwargs)

    async def _reload_disk(self, **kwargs):
        await self.middleware.call('etc.generate', 'fstab')
        await self._service("mountlate", "start", quiet=True, **kwargs)
        # Restarting rrdcached can take a long time. There is no
        # benefit in waiting for it, since even if it fails it will not
        # tell the user anything useful.
        asyncio.ensure_future(self.restart("collectd", kwargs))

    async def _reload_user(self, **kwargs):
        await self.middleware.call("etc.generate", "user")
        await self.middleware.call('etc.generate', 'aliases')
        await self.middleware.call('etc.generate', 'sudoers')
        await self.reload("cifs", kwargs)

    async def _restart_system_datasets(self, **kwargs):
        systemdataset = await self.middleware.call('systemdataset.setup')
        if not systemdataset:
            return None
        if systemdataset['syslog']:
            await self.restart("syslogd", kwargs)
        await self.restart("cifs", {'onetime': False})

        # Restarting rrdcached can take a long time. There is no
        # benefit in waiting for it, since even if it fails it will not
        # tell the user anything useful.
        # Restarting rrdcached will make sure that we start/restart collectd as well
        asyncio.ensure_future(self.restart("rrdcached", kwargs))

    @private
    async def identify_process(self, name):
        for service, definition in self.SERVICE_DEFS.items():
            if definition.procname == name:
                return service

    @accepts(Int("pid"), Int("timeout", default=10))
    def terminate_process(self, pid, timeout):
        """
        Terminate process by `pid`.

        First send `TERM` signal, then, if was not terminated in `timeout` seconds, send `KILL` signal.

        Returns `true` is process has been successfully terminated with `TERM` and `false` if we had to use `KILL`.
        """
        try:
            process = psutil.Process(pid)
        except psutil.NoSuchProcess:
            raise CallError("Process does not exist")

        process.terminate()

        gone, alive = psutil.wait_procs([process], timeout)
        if not alive:
            return True

        alive[0].kill()
        return False


def setup(middleware):
    middleware.event_register('service.query', 'Sent on service changes.')
