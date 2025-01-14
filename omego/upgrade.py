#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import shutil
import tempfile
import logging

from artifacts import Artifacts
from db import DbAdmin
from external import External
from yaclifw.framework import Command, Stop
import fileutils
from env import EnvDefault, DbParser, FileUtilsParser, JenkinsParser
from env import WINDOWS

log = logging.getLogger("omego.upgrade")


class Install(object):

    def __init__(self, cmd, args):

        self.args = args
        log.info("%s: %s", self.__class__.__name__, cmd)
        log.debug("Current directory: %s", os.getcwd())

        if cmd == 'upgrade':
            newinstall = False
            if not os.path.exists(args.sym):
                raise Stop(30, 'Symlink is missing: %s' % args.sym)
        elif cmd == 'install':
            newinstall = True
            if os.path.exists(args.sym):
                raise Stop(30, 'Symlink already exists: %s' % args.sym)
        else:
            raise Exception('Unexpected command: %s' % cmd)

        server_dir = self.get_server_dir()

        if newinstall:
            # Create a symlink to simplify the rest of the logic-
            # just need to check if OLD == NEW
            self.symlink(server_dir, args.sym)
            log.info("Installing %s (%s)...", server_dir, args.sym)
        else:
            log.info("Upgrading %s (%s)...", server_dir, args.sym)

        self.external = External(server_dir)
        self.external.setup_omero_cli()

        if not newinstall:
            self.external.setup_previous_omero_env(args.sym, args.savevarsfile)

        # Need lib/python set above
        import path
        self.dir = path.path(server_dir)

        if not newinstall:
            self.stop()
            self.archive_logs()

        copyold = not newinstall and not args.ignoreconfig
        self.configure(copyold, args.prestartfile)
        self.directories()

        if newinstall:
            self.init_db()

        self.upgrade_db()

        self.external.save_env_vars(args.savevarsfile, args.savevars.split())
        self.start()

    def get_server_dir(self):
        """
        Either downloads and/or unzips the server if necessary
        return: the directory of the unzipped server
        """
        if not self.args.server:
            if self.args.skipunzip:
                raise Stop(0, 'Unzip disabled, exiting')

            log.info('Downloading server')
            artifacts = Artifacts(self.args)
            server = artifacts.download('server')
        else:
            progress = 0
            if self.args.verbose:
                progress = 20
            ptype, server = fileutils.get_as_local_path(
                self.args.server, self.args.overwrite, progress=progress,
                httpuser=self.args.httpuser,
                httppassword=self.args.httppassword)
            if ptype == 'file':
                if self.args.skipunzip:
                    raise Stop(0, 'Unzip disabled, exiting')
                log.info('Unzipping %s', server)
                server = fileutils.unzip(
                    server, match_dir=True, destdir=self.args.unzipdir)

        log.debug('Server directory: %s', server)
        return server

    def stop(self):
        try:
            log.info("Stopping server")
            self.bin("admin status --nodeonly")
            self.bin("admin stop")
        except Exception as e:
            log.error('Error whilst stopping server: %s', e)

        if self.web():
            try:
                log.info("Stopping web")
                self.stopweb()
            except Exception as e:
                log.error('Error whilst stopping web: %s', e)

    def configure(self, copyold, prestartfile):
        def samecontents(a, b):
            # os.path.samefile is not available on Windows
            try:
                return os.path.samefile(a, b)
            except AttributeError:
                with open(a) as fa:
                    with open(b) as fb:
                        return fa.read() == fb.read()

        target = self.dir / "etc" / "grid" / "config.xml"

        if copyold:
            from path import path
            old_grid = path(self.args.sym) / "etc" / "grid"
            old_cfg = old_grid / "config.xml"
            log.info("Copying old configuration from %s", old_cfg)
            if not old_cfg.exists():
                raise Stop(40, 'config.xml not found')
            if target.exists() and samecontents(old_cfg, target):
                # This likely is caused by the symlink being
                # created early on an initial install.
                pass
            else:
                old_cfg.copy(target)
        else:
            if target.exists():
                log.info('Deleting configuration file %s', target)
                target.remove()

        if prestartfile:
            for f in prestartfile:
                log.info('Loading prestart file %s', f)
                ftype, fpath = fileutils.get_as_local_path(f, 'backup')
                if ftype != 'file':
                    raise Stop(50, 'Expected file, found: %s %s' % (
                        ftype, f))
                self.run(['load', fpath])

        self.configure_ports()

    def configure_ports(self):
        # Set registry, TCP and SSL ports
        self.run(["admin", "ports", "--skipcheck", "--registry",
                 self.args.registry, "--tcp",
                 self.args.tcp, "--ssl", self.args.ssl])

    def archive_logs(self):
        if self.args.archivelogs:
            logdir = os.path.join(self.args.sym, 'var', 'log')
            archive = self.args.archivelogs
            log.info('Archiving logs to %s', archive)
            fileutils.zip(archive, logdir, os.path.join(self.args.sym, 'var'))
            return archive

    def directories(self):
        if self.samedir(self.dir, self.args.sym):
            log.warn("Upgraded server was the same, not deleting")
            return

        try:
            target = self.readlink(self.args.sym)
            targetzip = target + '.zip'
        except IOError:
            log.error('Unable to get symlink target: %s', self.args.sym)
            target = None
            targetzip = None

        if "false" == self.args.skipdelete.lower() and target:
            try:
                log.info("Deleting %s", target)
                shutil.rmtree(target)
            except OSError as e:
                log.error("Failed to delete %s: %s", target, e)

        if "false" == self.args.skipdeletezip.lower() and targetzip:
            try:
                log.info("Deleting %s", targetzip)
                os.unlink(targetzip)
            except OSError as e:
                log.error("Failed to delete %s: %s", targetzip, e)

        self.rmlink(self.args.sym)
        self.symlink(self.dir, self.args.sym)

    def init_db(self):
        if self.args.initdb:
            log.debug('Initialising database')
            DbAdmin(self.dir, 'init', self.args, self.external)

    def upgrade_db(self):
        if self.args.upgradedb:
            log.debug('Upgrading database')
            DbAdmin(self.dir, 'upgrade', self.args, self.external)

    def start(self):
        self.run("admin start")
        if self.web():
            log.info("Starting web")
            self.startweb()

    def run(self, command):
        """
        Runs a command as if from the command-line
        without the need for using popen or subprocess
        """
        if isinstance(command, basestring):
            command = command.split()
        else:
            command = list(command)
        self.external.omero_cli(command)

    def bin(self, command):
        """
        Runs the omero command-line client with an array of arguments using the
        old environment
        """
        if isinstance(command, basestring):
            command = command.split()
        self.external.omero_bin(command)

    def web(self):
        return "false" == self.args.skipweb.lower()


class UnixInstall(Install):

    def stopweb(self):
        self.bin("web stop")

    def startweb(self):
        self.run("web start")

    def samedir(self, targetdir, link):
        return os.path.samefile(targetdir, link)

    def readlink(self, link):
        return os.path.normpath(os.readlink(link))

    def rmlink(self, link):
        try:
            os.unlink(link)
        except OSError as e:
            log.error("Failed to unlink %s: %s", link, e)
            raise

    def symlink(self, targetdir, link):
        try:
            os.symlink(targetdir, link)
        except OSError as e:
            log.error("Failed to symlink %s to %s: %s", targetdir, link, e)
            raise


class WindowsInstall(Install):

    def stopweb(self):
        log.info("Removing web from IIS")
        self.bin("web iis --remove")
        self.iisreset()

    def startweb(self):
        log.info("Configuring web in IIS")
        self.run("web iis")
        self.iisreset()

    # os.path.samefile doesn't work on Python 2
    # Create a tempfile in one directory and test for it's existence in the
    # other

    def samedir(self, targetdir, link):
        try:
            return os.path.samefile(targetdir, link)
        except AttributeError:
            with tempfile.NamedTemporaryFile(dir=targetdir) as test:
                return os.path.exists(
                    os.path.join(link, os.path.basename(test.name)))

    # Symlinks are a bit more complicated on Windows:
    # - You must have (elevated) administrator privileges
    # - os.symlink doesn't work on Python 2, you must use a win32 call
    # - os.readlink doesn't work on Python 2, and the solution suggested in
    #   http://stackoverflow.com/a/7924557 doesn't work for me.
    #
    # We need to dereference the symlink in order to delete the old server
    # so for now just store it in a text file alongside the symlink.

    def readlink(self, link):
        try:
            return os.path.normpath(os.readlink(link))
        except AttributeError:
            with open('%s.target' % link, 'r') as f:
                return os.path.normpath(f.read())

    def rmlink(self, link):
        """
        """
        if os.path.isdir(link):
            os.rmdir(link)
        else:
            os.unlink(link)

    def symlink(self, targetdir, link):
        """
        """
        try:
            os.symlink(targetdir, link)
        except AttributeError:
            import win32file
            flag = 1 if os.path.isdir(targetdir) else 0
            try:
                win32file.CreateSymbolicLink(link, targetdir, flag)
            except Exception as e:
                log.error(
                    "Failed to symlink %s to %s: %s", targetdir, link, e)
                raise
            with open('%s.target' % link, 'w') as f:
                f.write(targetdir)

    def iisreset(self):
        """
        Calls iisreset
        """
        self.external.run('iisreset', [])


class InstallBaseCommand(Command):
    """
    Base command class to install or upgrade an OMERO server
    Do not call this class directly
    """

    def __init__(self, sub_parsers):
        super(InstallBaseCommand, self).__init__(sub_parsers)

        self.parser.add_argument("-n", "--dry-run", action="store_true")
        self.parser.add_argument(
            "server", nargs="?", help="The server directory, or a server-zip, "
            "or the url of a server-zip")

        self.parser.add_argument(
            "--prestartfile", action="append",
            help="Run these OMERO commands before starting server, "
                 "can be repeated")
        self.parser.add_argument(
            "--ignoreconfig", action="store_true",
            help="Don't copy the old configuration file when upgrading")

        self.parser = JenkinsParser(self.parser)
        self.parser = DbParser(self.parser)
        self.parser = FileUtilsParser(self.parser)

        Add = EnvDefault.add

        # Ports
        Add(self.parser, "prefix", "")
        Add(self.parser, "registry", "%(prefix)s4061")
        Add(self.parser, "tcp", "%(prefix)s4063")
        Add(self.parser, "ssl", "%(prefix)s4064")

        Add(self.parser, "sym", "OMERO-CURRENT")

        Add(self.parser, "skipweb", "false")
        Add(self.parser, "skipdelete", "true")
        Add(self.parser, "skipdeletezip", "false")

        # Record the values of these environment variables in a file
        envvars = "ICE_HOME PATH DYLD_LIBRARY_PATH LD_LIBRARY_PATH PYTHONPATH"
        envvarsfile = os.path.join("%(sym)s", "omero.envvars")
        Add(self.parser, "savevars", envvars)
        Add(self.parser, "savevarsfile", envvarsfile)

    def __call__(self, args):
        super(InstallBaseCommand, self).__call__(args)
        self.configure_logging(args)

        # Since EnvDefault.__action__ is only called if a user actively passes
        # a variable, there's no way to do the string replacing in the action
        # itself. Instead, we're post-processing them here, but this could be
        # improved.

        names = sorted(x.dest for x in self.parser._actions)
        for dest in names:
            if dest in ("help", "verbose", "quiet"):
                continue
            value = getattr(args, dest)
            if value and isinstance(value, basestring):
                replacement = value % dict(args._get_kwargs())
                log.debug("% 20s => %s" % (dest, replacement))
                setattr(args, dest, replacement)

        if args.dry_run:
            return

        if WINDOWS:
            WindowsInstall(self.NAME, args)
        else:
            UnixInstall(self.NAME, args)


class InstallCommand(InstallBaseCommand):
    """
    Setup a new OMERO installation.
    """

    NAME = "install"

    def __init__(self, sub_parsers):
        super(InstallCommand, self).__init__(sub_parsers)
        group = self.parser.parser.add_mutually_exclusive_group()
        group.add_argument(
            "--initdb", action="store_true", help="Initialise the database")
        group.add_argument(
            "--upgradedb", action="store_true", help="Upgrade the database")


class UpgradeCommand(InstallBaseCommand):
    """
    Upgrade an existing OMERO installation.
    """

    NAME = "upgrade"

    def __init__(self, sub_parsers):
        super(UpgradeCommand, self).__init__(sub_parsers)
        self.parser.add_argument(
            "--upgradedb", action="store_true", help="Upgrade the database")
        self.parser.add_argument(
            "--archivelogs", default=None, help=(
                "Archive the logs directory to this zip file, "
                "overwriting if it exists"))
