from .base import SimpleService


class FTPService(SimpleService):
    name = "ftp"

    etc = ["proftpd"]

    freebsd_rc = "proftpd"
    freebsd_pidfile = "/var/run/proftpd.pid"
