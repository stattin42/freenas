from .base import SimpleService


class MDNSService(SimpleService):
    name = "mdns"
    reloadable = True

    etc = ["mdns"]

    freebsd_rc = "avahi-daemon"
    freebsd_pidfile = "/var/run/avahi-daemon/pid"
