from __future__ import (unicode_literals, print_function, absolute_import,
                        division)

import logging
import json
# Py2-3 compatibility
try:
    import queue
except ImportError:
    import Queue as queue
import os
import platform
import random
import string
import subprocess
import sys
import tempfile
import threading
from time import sleep

from six import reraise as raise_
from six.moves import zip_longest
import paramiko
from dfs_sdk import scaffold

from dbmp.topology import get_topology

DBMP_REPO = 'http://github.com/Datera/dbmp'
ASSETS = os.path.join(
        os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))), 'assets')
LOCKS = {}


class Parallel(object):

    """
    A helper class that makes it simpler to run tasks multiple times
    in parallel.  If you have multiple tasks you want to run in parallel
    you need to encapsulate them in a single function that accepts a variety
    of arguments.
    """

    def __init__(self, funcs, args_list=None, kwargs_list=None, max_workers=5,
                 timeout=3600):
        """

        :param funcs: A list of functions to be used by the workers
        :param args_list: A list of tuples of arguments required by each
                          function in `funcs`
        :param kwargs_list: A list of dictionaries of kwargs accepted
                            by each function in `funcs`
        :param max_workers: The maximum number of simultaneous threads
        """
        self.logger = logging.getLogger(__name__)
        if not self.logger.handlers:
            self.logger.addHandler(logging.NullHandler())
        self.funcs = funcs
        self.args_list = args_list if args_list else []
        self.kwargs_list = kwargs_list if kwargs_list else []
        self.max_workers = max_workers
        self.queue = queue.Queue()
        self.exceptions = queue.Queue()
        self.threads = []
        self.timeout = timeout
        self.keep_running = True

    @staticmethod
    def _set_current_thread_name_from_func_name(func):
        """ Renames the current thread to reflect the name of func """
        orig_thread_number = threading.current_thread().name.split('-')[-1]
        threading.current_thread().name = "Parallel-" + \
            func.__module__ + '.' + func.__name__ + "-" + orig_thread_number

    def _wrapped(self):
        threading.current_thread().name = "Parallel-Worker-" + \
            threading.current_thread().name
        while self.keep_running:
            try:
                func, args, kwargs = self.queue.get(block=False)
            except queue.Empty:
                break
            self.logger.debug(
                "Running {} with args: {} and kwargs {} with thread {}".format(
                    func, args, kwargs, threading.current_thread()))
            try:
                # Rename this thread to reflect the function we're running
                orig_name = threading.current_thread().name
                self._set_current_thread_name_from_func_name(func)
                # Call the function:
                func(*args, **kwargs)
                # Reset this thread name to its original (e.g. "Thread-9")
                threading.current_thread().name = orig_name
            except Exception:
                self.keep_running = False
                self.logger.exception("Exception occurred in thread {}".format(
                    threading.current_thread()))
                self.exceptions.put(sys.exc_info())

            self.queue.task_done()

    def run_threads(self):
        """
        Call this function to start the worker threads.  They will continue
        running until all args/kwargs are consumed.  This is a blocking call.
        """
        try:
            for func, args, kwargs in zip_longest(
                    self.funcs, self.args_list, self.kwargs_list,
                    fillvalue={}):
                # Flag a common (and confusing) user error:
                if isinstance(args, str) or isinstance(args, unicode):
                    msg = "args_list must be list of lists not list of strings"
                    raise ValueError(msg)
                self.queue.put((func, args, kwargs))

            for _ in xrange(self.max_workers):
                thread = threading.Thread(target=self._wrapped)
                thread.setDaemon(True)
                thread.start()
                self.threads.append(thread)

            if (len(self.funcs) < len(self.args_list) or
                    len(self.funcs) < len(self.kwargs_list)):
                raise ValueError(
                    "List of functions passed into a Parallel object must "
                    "be longer or equal in length to the list of args "
                    "and/or kwargs passed to the object.  {}, {}, {"
                    "}".format(self.funcs, self.args_list, self.kwargs_list))

            while self.queue.unfinished_tasks:
                # Check if exception has been generated by a thread and raise
                # if found one is found
                try:
                    exc = self.exceptions.get(block=False)
                    self.keep_running = False
                    raise_(*exc)
                except queue.Empty:
                    pass
                sleep(0.2)

        # Ensure all threads will exit regardless of the current
        # state of the main thread
        finally:

            try:
                exc = self.exceptions.get(block=False)
                self.keep_running = False
                # Join all threads to ensure we don't continue
                # without all threads stopping
                for thread in self.threads:
                    thread.join(self.timeout)
                raise_(*exc)
            except queue.Empty:
                pass


def exe(cmd, fail_ok=False):
    cmd = '{{ {}; }} 2>/dev/null'.format(cmd)
    dprint("Running command:", cmd)
    try:
        # Redirect stderr
        return subprocess.check_output(cmd, shell=True)
    except subprocess.CalledProcessError as e:
        if fail_ok:
            dprint(e)
            return None
        raise EnvironmentError(
            "Encountered error running command: {}, error : {}".format(cmd, e))


def get_ssh(host):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(
        paramiko.AutoAddPolicy())
    user, ip, creds = get_topology(host)
    if os.path.exists(creds):
        ssh.connect(hostname=ip,
                    username=user,
                    banner_timeout=60,
                    pkey=paramiko.RSAKey.from_private_key_file(creds))
    else:
        ssh.connect(hostname=ip,
                    username=user,
                    password=creds,
                    banner_timeout=60)
    return ssh


def exe_remote(host, cmd, fail_ok=False):
    dprint("Running remote command {} on host {}:".format(cmd, host))
    ssh = get_ssh(host)
    _, stdout, stderr = ssh.exec_command(cmd)
    exit_status = stdout.channel.recv_exit_status()
    result = None
    if int(exit_status) == 0:
        result = stdout.read().decode('utf-8')
    elif fail_ok:
        result = stderr.read().decode('utf-8')
    else:
        raise EnvironmentError(
            "Nonzero return code: {} stderr: {}".format(
                exit_status,
                stderr.read().decode('utf-8')))
    ssh.close()
    return result


def exe_remote_py(host, cmd):
    prefix = ('cd ~/dbmp/src && ~/dbmp/.dbmp/bin/python '
              '~/dbmp/src/dbmp/remote/{}')
    return exe_remote(host, prefix.format(cmd))


def check_install(host):
    try:
        exe_remote(host, 'test -d ~/dbmp')
    except EnvironmentError:
        exe_remote(host, 'git clone {} && ~/dbmp/install.py'.format(DBMP_REPO))
    with tempfile.NamedTemporaryFile() as tf:
        config = scaffold.get_config()
        tf.write(json.dumps(config))
        tf.flush()
        user, _, _ = get_topology(host)
        putf_remote(host, tf.name,
                    '/home/{}/datera-config.json'.format(user))


def putf_remote(host, local, file):
    ssh = get_ssh(host)
    sftp = ssh.open_sftp()
    if hasattr(local, 'read'):
        sftp.putfo(local, file)
    else:
        sftp.put(local, file)
    sftp.close()


def rand_file_name(directory):
    return os.path.join(directory, ''.join([
        random.choice(string.ascii_letters) for _ in range(10)]))


def get_hostname(host):
    if host == 'local':
        hostname = platform.node().strip()
    else:
        hostname = exe_remote(host, "hostname").strip()
    return hostname


def dprint(*args, **kwargs):
    if scaffold.VERBOSE:
        print(*args, **kwargs)


def locker(func):
    def _wrapper(*args, **kwargs):
        n = func.__name__
        if n not in LOCKS:
            LOCKS[n] = threading.Lock()
        with LOCKS[n]:
            return func(*args, **kwargs)
    return _wrapper
