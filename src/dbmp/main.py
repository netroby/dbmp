#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import unicode_literals, print_function, division

import argparse
import json
import sys
import textwrap

from dfs_sdk import scaffold
# from dfs_sdk import exceptions as dexceptions

from dbmp.metrics import get_metrics, write_metrics
from dbmp.mount import mount_volumes, mount_volumes_remote, clean_mounts
from dbmp.mount import clean_mounts_remote, list_mounts
from dbmp.fio import gen_fio, gen_fio_remote
from dbmp.utils import exe
from dbmp.volume import create_volumes, clean_volumes, list_volumes
from dbmp.volume import list_templates

SUCCESS = 0
FAILURE = 1

METRIC_CHOICES = ('reads', 'writes', 'bytes_read',
                  'bytes_written', 'iops_read', 'iops_write',
                  'thpt_read', 'thpt_write', 'lat_avg_read',
                  'lat_avg_write', 'lat_50_read', 'lat_90_read',
                  'lat_100_read', 'lat_50_write',
                  'lat_90_write', 'lat_100_write')


def hf(txt):
    return textwrap.fill(txt)


def run_health(api):
    config = scaffold.get_config()
    try:
        exe('ping -c 1 -w 1 {}'.format(config['mgmt_ip']))
    except EnvironmentError:
        print('Could not ping mgmt_ip:', config['mgmt_ip'])
        return False
    try:
        api.app_instances.list()
    except Exception as e:
        print("Could not connect to cluster", e)
        return False
    npass = True
    av = api.system.network.access_vip.get()
    for np in av['network_paths']:
        ip = np.get('ip')
        if ip:
            try:
                exe('ping -c 1 -w 1 {}'.format(ip))
            except EnvironmentError:
                print('Could not ping: {} {}'.format(np.get('name'), ip))
                npass = False
    if not npass:
        return False
    print("Health Check Completed Successfully")
    return True


def main(args):
    api = scaffold.get_api()
    print('Using Config:')
    scaffold.print_config()

    if args.health:
        if not run_health(api):
            return FAILURE
        return SUCCESS

    if 'volumes' in args.list:
        for vol in args.volume:
            list_volumes(args.run_host, api, vol, detail='detail' in args.list)
        return SUCCESS
    elif 'templates' in args.list:
        list_templates(api, detail='detail' in args.list)
    elif 'mounts' in args.list:
        for vol in args.volume:
            list_mounts(args.run_host, api, vol, 'detail' in args.list,
                        not args.no_multipath)
        return SUCCESS

    if any((args.unmount, args.logout, args.clean)):
        for vol in args.volume:
            if args.run_host == 'local':
                clean_mounts(api, vol, args.directory, args.workers)
            else:
                clean_mounts_remote(
                    args.run_host, vol, args.directory, args.workers)
            if args.unmount:
                return SUCCESS
    if args.clean:
        for vol in args.volume:
            clean_volumes(api, vol, args.workers)
        return SUCCESS
    if args.logout:
        return SUCCESS

    vols = None
    for vol in args.volume:
        vols = create_volumes(args.run_host, api, vol, args.workers)

    login_only = not args.mount and args.login
    if (args.mount or args.login) and vols and args.run_host == 'local':
        dev_or_folders = mount_volumes(
            api, vols, not args.no_multipath, args.fstype, args.fsargs,
            args.directory, args.workers, login_only)
    elif (args.mount or args.login) and vols and args.run_host != 'local':
        dev_or_folders = mount_volumes_remote(
            args.run_host, vols, not args.no_multipath, args.fstype,
            args.fsargs, args.directory, args.workers, login_only)

    if args.fio:
        try:
            exe("which fio")
        except EnvironmentError:
            print("FIO is not installed")
    if args.fio and (not args.mount and not args.login):
        print("--mount or --login MUST be specified when using --fio")
    elif args.fio and args.run_host == 'local':
        gen_fio(args.fio_workload, dev_or_folders)
    elif args.fio and args.run_host != 'local':
        gen_fio_remote(args.run_host, args.fio_workload, dev_or_folders)

    if args.metrics:
        data = None
        try:
            interval, timeout = map(int, args.metrics.split(','))
            if interval < 1 or timeout < 1:
                raise ValueError()
            mtypes = args.metrics_type
            if not args.metrics_type:
                mtypes = ['iops_write']
            data = get_metrics(
                api, mtypes, args.volume, interval, timeout,
                args.metrics_op)
        except ValueError:
            print("--metrics argument must be in format '--metrics i,t' where"
                  "'i' is the interval in seconds and 't' is the timeout in "
                  "seconds.  Both must be positive integers >= 1")
            return FAILURE
        if data:
            write_metrics(data, args.metrics_out_file)
        else:
            print("No data recieved from metrics")
            return FAILURE
    return SUCCESS


if __name__ == '__main__':
    tparser = scaffold.get_argparser(add_help=False)
    parser = argparse.ArgumentParser(
        parents=[tparser], formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('--run-host', default='local',
                        help=hf('Host on which targets should be logged in.'
                                ' This value will be a key in your '
                                '"dbmp-topology.json" file. Use "local" for'
                                'the current host'))
    parser.add_argument('--health', action='store_true',
                        help='Run a quick health check')
    parser.add_argument('--list', choices=('volumes', 'volumes-detail',
                                           'templates', 'templates-detail',
                                           'mounts', 'mounts-detail'),
                        default='',
                        help='List accessible Datera Resources')
    parser.add_argument('--volume', action='append', default=[],
                        help='Supports the following comma separated params:\n'
                             ' \n'
                             '* prefix, default=--run-default hostname\n'
                             '* count (num created), default=1\n'
                             '* size (GB), default=1\n'
                             '* replica, default=3\n'
                             '* <any supported qos param, eg: read_iops_max>\n'
                             '* placement_mode, default=hybrid\n'
                             '      choices: hybrid|single_flash|all_flash\n'
                             '* template, default=None\n \n'
                             'Example: prefix=test,size=2,replica=2\n \n'
                             'Alternatively a json file with the above\n'
                             'parameters can be specified')
    parser.add_argument('--login', action='store_true',
                        help='Login volumes (implied by --mount)')
    parser.add_argument('--logout', action='store_true',
                        help='Logout volumes (implied by --unmount)')
    parser.add_argument('--mount', action='store_true',
                        help='Mount volumes, (implies --login)')
    parser.add_argument('--unmount', action='store_true',
                        help='Unmount volumes only.  Does not delete volume')
    parser.add_argument('--clean', action='store_true',
                        help='Deletes volumes (implies --unmount and '
                             '--logout)')
    parser.add_argument('--workers', default=5, type=int,
                        help='Number of worker threads for this action')
    parser.add_argument('--no-multipath', action='store_true')
    parser.add_argument('--fstype', default='xfs',
                        help='Filesystem to use when formatting devices')
    parser.add_argument('--fsargs', default='',
                        help=hf('Extra args to give formatter, eg "-E '
                                'lazy_table_init=1".  Make sure fstype matches'
                                ' the args you are passing in'))
    parser.add_argument('--directory', default='/mnt',
                        help='Directory under which to mount devices')
    parser.add_argument('--fio', action='store_true',
                        help='Run fio workload against mounted volumes')
    parser.add_argument('--fio-workload',
                        help='Fio workload file to use.  If not specified, '
                             'default workload will be used')
    parser.add_argument('--metrics', help=hf(
                        'Run metrics with specified report interval and '
                        'timeout in seconds --metrics 5,60 would get metrics '
                        'every 5 seconds for 60 seconds'))
    parser.add_argument('--metrics-type',
                        metavar='',
                        action='append',
                        default=[],
                        choices=METRIC_CHOICES,
                        help=hf('Metric to retrieve.  Choices: {}'.format(
                                json.dumps(METRIC_CHOICES))))
    parser.add_argument('--metrics-op',
                        choices=(None, 'average', 'max', 'min',
                                 'total-average', 'total-max', 'total-min'),
                        help='Operation to perform on metrics data.  For '
                             'example: Averaging the results')
    parser.add_argument('--metrics-out-file', default='metrics-out.json',
                        help='Output file for metrics report.  Use "stdout" to'
                        ' print metrics to STDOUT')
    args = parser.parse_args()
    sys.exit(main(args))
