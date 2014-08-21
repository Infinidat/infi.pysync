import os
import re
import stat
import errno
import paramiko
import getpass
from contextlib import closing


def _null_logger(*args):
    pass


class SyncSFTPClient(paramiko.SFTPClient):
    def __init__(self, user, host, port, path, dry_run, preserve_time=True, identity_file=None,
                 logger_func=_null_logger, preserve_permissions=True):
        self.user = user
        self.host = host
        self.port = port
        self.path = path
        self.known_hosts = None
        self.id_files = [] if not identity_file else [identity_file]
        self.password = None
        self.dry_run = dry_run
        self.preserve_time = preserve_time
        self.preserve_permissions = preserve_permissions
        self.logger_func = logger_func

        if not self.path:
            self.path = "~"
        self.port = int(self.port) if self.port else 22

        self._parse_ssh_config()
        if not self.user:
            default_user = getpass.getuser()
            self.user = raw_input('Username [{}]: '.format(default_user))
            if not self.user:
                self.user = default_user

        transport = paramiko.Transport((self.host, self.port))
        transport.start_client()

        self._check_known_host_keys(transport.get_remote_server_key())
        if not self._try_id_files(transport):
            if not self.password:
                self.password = getpass.getpass('Password for {}@{}: '.format(self.user, self.host))
            transport.auth_password(self.user, self.password)

        chan = transport.open_session()
        if chan is None:
            raise Exception("failed to open sftp (channel is none)")
        chan.invoke_subsystem('sftp')

        super(SyncSFTPClient, self).__init__(chan)

    def remote_path(self):
        remote_homedir = self.normalize(".")  # find the home dir
        path = self.path

        if path.startswith("~"):
            path = path.replace("~", remote_homedir, 1)
        return self.normalize(os.path.normpath(path))

    def remove(self, path):
        self.logger_func("rm remote:{}", path)
        if not self.dry_run:
            return super(SyncSFTPClient, self).remove(path)

    def mkdir(self, path):
        self.logger_func("mkdir remote:{}", path)
        if not self.dry_run:
            return super(SyncSFTPClient, self).mkdir(path)

    def put(self, src, dst):
        self.logger_func("cp {} remote:{}", src, dst)
        if not self.dry_run:
            src_stat = os.lstat(src)
            super(SyncSFTPClient, self).put(src, dst)
            if self.preserve_time:
                self.utime(dst, (src_stat.st_atime, src_stat.st_mtime))
            if self.preserve_permissions:
                permissions = os.stat(src)[stat.ST_MODE]
                self.chmod(dst, permissions)

    def mkdir_if_not_exist(self, path):
        try:
            remote_stat = self.stat(path)
            if not stat.S_ISDIR(remote_stat.st_mode):
                raise Exception("remote path {} should be a directory, but it's not.".format(path))
        except IOError, e:
            if e.errno == errno.ENOENT:
                self.mkdir(path)
            else:
                raise

    def rm(self, path):
        try:
            self.logger_func("rm remote:{}", path)
            self.remove(path)
        except IOError, e:
            if e.errno != errno.ENOENT:
                raise

    def _rm_rf_dir(self, path):
        stat_attrs = self.listdir_attr(path)
        for o in stat_attrs:
            child_path = os.path.join(path, o.filename)
            if stat.S_ISDIR(o.st_mode):
                self._rm_rf_dir(child_path)
            else:
                super(SyncSFTPClient, self).remove(child_path)

        super(SyncSFTPClient, self).rmdir(path)

    def rm_rf(self, path):
        self.logger_func("rm -rf remote:{}", path)
        if self.dry_run:
            return

        s = self.stat_or_none(path)
        if not s:
            return

        if stat.S_ISDIR(s.st_mode):
            self._rm_rf_dir(path)
        else:
            super(SyncSFTPClient, self).remove(path)

    def stat_or_none(self, path):
        try:
            return self.stat(path)
        except IOError, e:
            if e.errno != errno.ENOENT:
                raise
            return None

    def _parse_ssh_config(self):
        ssh_config_path = os.path.expanduser("~/.ssh/config")

        if not os.path.exists(ssh_config_path):
            return

        ssh_config = paramiko.SSHConfig()
        with closing(open(ssh_config_path, "r")) as f:
            ssh_config.parse(f)

        host_config = ssh_config.lookup(self.host)
        if not self.user and 'user' in host_config:
            self.user = host_config['user']
        if 'identityfile' in host_config:
            self.id_files.extend(host_config['identityfile'])
        if not self.port and 'port' in host_config:
            self.port = int(host_config['port'])
        if 'userknownhostsfile' in host_config:
            self.known_hosts = host_config['userknownhostsfile']

    def _check_known_host_keys(self, key):
        """If we have a known_hosts file, try to see that the server has the same credentials if it's there."""
        default_known_hosts = os.path.expanduser("~/.ssh/known_hosts")
        if not self.known_hosts and os.path.exists(default_known_hosts):
            self.known_hosts = default_known_hosts

        if not self.known_hosts or not os.path.exists(self.known_hosts):
            return

        known_host_key, known_host_key_type = None, None
        try:
            known_host_keys = paramiko.util.load_host_keys(os.path.expanduser(self.known_hosts))
            if self.host in known_host_keys:
                known_host_key_type = known_host_keys[self.host].keys()[0]
                known_host_key = known_host_keys[self.host][known_host_key_type]

                if known_host_key.get_name() != key.get_name() or str(known_host_key) != str(key):
                    raise Exception("Known host key verification mismatch, aborting.")
        except IOError:
            print("unable to read known hosts file {}".format(self.known_hosts))

    def _try_id_files(self, transport):
        """Try to authenticate with all the identity files at our disposal."""
        default_id = os.path.expanduser("~/.ssh/id_rsa")
        if not self.id_files and os.path.exists(default_id):
            self.id_files.append(default_id)

        for path in self.id_files:
            if not os.path.exists(path):
                continue

            try:
                pkey = paramiko.DSSKey.from_private_key_file(path)
            except paramiko.PasswordRequiredException:
                password = getpass.getpass('Password for private key {}: '.format(path))
                pkey = paramiko.DSSKey.from_private_key_file(path, password)
            except paramiko.SSHException:
                try:
                    pkey = paramiko.RSAKey.from_private_key_file(path)
                except paramiko.PasswordRequiredException:
                    password = getpass.getpass('Password for private key {}: '.format(path))
                    pkey = paramiko.RSAKey.from_private_key_file(path, password)
            except IOError:
                print("failed reading pkey file {}, skipping it.".format(path))

            try:
                transport.auth_publickey(self.user, pkey)
                return True
            except:
                pass
        return False

    @classmethod
    def _parse_target(cls, s):
        """Parse an scp-like string (e.g. user@host:port:path)"""
        match = re.match(r"(?:([^@\:\/]+)@)?([^:\/]+)(?:\:(\d+))?(?:\:(.*))?", s)
        if not match:
            raise Exception("target {} is not a valid ssh path (user@host:path|host:path|host)".format(s))
        user = match.group(1)
        host = match.group(2)
        port = match.group(3)
        path = match.group(4)
        return (user, host, port, path)

    @classmethod
    def create_sftp_connection(cls, target, dry_run=False, preserve_time=True, identity_file=None,
                               logger_func=_null_logger, preserve_permissions=True):
        global verbose

        user, host, port, path = cls._parse_target(target)
        return SyncSFTPClient(user, host, port, path, dry_run, preserve_time, identity_file, logger_func,
                              preserve_permissions)
