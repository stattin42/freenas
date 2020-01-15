from .base import SimpleService


class RsyncService(SimpleService):
    name = "rsync"

    etc = ["rsync"]

    freebsd_rc = "rsync"
    freebsd_pidfile = "/var/run/rsyncd.pid"
