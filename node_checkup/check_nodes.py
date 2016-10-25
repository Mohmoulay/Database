#!/usr/bin/python
# -*- coding: utf-8 -*-

# Author: Jonas Karlsson
# Date: October 2016
# License: GNU General Public License v3
# Developed for use by the EU H2020 MONROE project

r"""
Will try to determine which nodes that regularly contribute to the database.

Will look in all tables and see what nodes has contributed data.
"""
import json
import time
import datetime
import os
import sys
import argparse
import textwrap
import syslog

from cassandra.cluster import Cluster
# from cassandra.query import Statement
from cassandra.query import dict_factory, ordered_dict_factory
from cassandra import ConsistencyLevel
from cassandra import InvalidRequest
from cassandra.auth import PlainTextAuthProvider

CMD_NAME = os.path.basename(__file__)


def create_arg_parser():
    """Create a argument parser and return it."""
    parser = argparse.ArgumentParser(
        prog=CMD_NAME,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent('''
            Parses .json or (.xz) files in in_dir and inserts them into the
            Cassandra Cluster specified in -H/--hosts.
            All directories not existing will be created.'''))
    parser.add_argument('-u', '--user',
                        help="Cassandra username")
    parser.add_argument('-p', '--password',
                        help="Cassandra password")
    parser.add_argument('-H', '--hosts',
                        nargs='+',
                        default=["127.0.0.1"],
                        help="Hosts in the cluster (default 127.0.0.1)")
    parser.add_argument('-k', '--keyspace',
                        required=True,
                        help="Keyspace to use")
    parser.add_argument('-t', '--timespan',
                        required=True,
                        help=("Xw Xd Xm "
                              "What timespan to look for (now-timespan)"))
    parser.add_argument('--authenv',
                        action="store_true",
                        help=("Use environment variables MONROE_DB_USER and "
                              "MONROE_DB_PASSWD as username and password"))
    parser.add_argument('-v', '--version',
                        action="version",
                        version="%(prog)s 1.0")
    return parser


def parse_special_args(args, parser, now):
    """Parse and varifies user,password and failed,processed dirs."""
    db_user = None
    db_password = None
    if not args.authenv and not (args.user and args.password):
        parser.error('either --authenv or -u/--user USER and -p/--password '
                     'PASSWORD needs to be defined')

    if args.authenv:
        if 'MONROE_DB_USER' not in os.environ:
            parser.error("missing user env MONROE_DB_USER")
        if 'MONROE_DB_PASSWD' not in os.environ:
            parser.error("missing user env MONROE_DB_PASSWD")
        db_user = os.environ['MONROE_DB_USER']
        db_password = os.environ['MONROE_DB_PASSWD']

    # Specified user and password takes precedence over environment variables
    if args.user:
        db_user = args.user
    if args.password:
        db_password = args.password

    if "w" in args.timespan:
        weeks = int(args.timespan.strip('w'))
        span = weeks*3600*24*7
    elif "d" in args.timespan:
        days = int(args.timespan.strip('d'))
        span = days*3600*24
    elif "h" in args.timespan:
        hours = int(args.timespan.strip('h'))
        span = hours*3600
    elif "m" in args.timespan:
        minutes = int(args.timespan.strip('m'))
        span = hours*60
    else:
        span = int(args.timespan)
    return (int(now - span), db_user, db_password)

if __name__ == '__main__':
    parser = create_arg_parser()
    args = parser.parse_args()
    now = time.time()
    span, db_user, db_password = parse_special_args(args, parser, now)

    # Assuming default port: 9042, clusters and sessions are longlived and
    # should be reused
    session = None
    cluster = None
    results = {}
    auth = PlainTextAuthProvider(username=db_user, password=db_password)
    cluster = Cluster(args.hosts, auth_provider=auth)
    session = cluster.connect(args.keyspace)
    #session.row_factory = dict_factory
    session.row_factory = ordered_dict_factory
    keyspace = cluster.metadata.keyspaces[args.keyspace]
    table_names = keyspace.tables.keys()
    # Check for tables not containing obligatory keys
    print "{:#<80.80}".format("#### These tables does not contain "
           "timestamp or/and nodeid ")
    for table_name in table_names:
        col_names = keyspace.tables[table_name].columns.keys()
        if 'timestamp' not in col_names or 'nodeid' not in col_names:
            print ("{}").format(table_name)
    print "{:#<80}".format("")
    simple_query = {}
    error_query = {}
    spec_query = {}
    # These queries are only used to get nodeid seen in the db
    tables = ['monroe_meta_node_sensor',
              'monroe_exp_exhaustive_paris',
              'monroe_exp_simple_traceroute',
              'monroe_meta_node_event']
    for table_name in ['monroe_meta_node_sensor',
                       'monroe_exp_exhaustive_paris',
                       'monroe_exp_simple_traceroute',
                       'monroe_meta_node_event']:
        simple_query[table_name] = ('SELECT distinct nodeid from '
                                    '{} where timestamp > {} '
                                    'ALLOW FILTERING').format(table_name, span)

    # Only check for errors
    error_query['monroe_meta_node_event'] = ('SELECT distinct nodeid '
                                             'from monroe_meta_node_event '
                                             'where timestamp > {} '
                                             'AND eventtype = '
                                             '\'Watchdog.Failed\' '
                                             'ALLOW FILTERING').format(span)

    # Specific checks
    GPS = 'monroe_meta_device_gps'
    PING = 'monroe_exp_ping'
    MODEM = 'monroe_meta_device_modem'
    spec_query[PING] = ('SELECT nodeid, '
                        'timestamp, '
                        'operator, '
                        'iccid '
                        'from {} '
                        'where timestamp > {} '
                        'ALLOW FILTERING').format(PING, span)
    spec_query[MODEM] = ('SELECT nodeid, '
                         'timestamp, '
                         'operator, '
                         'iccid, '
                         'internalipaddress, '
                         'internalinterface, '
                         'ipaddress, '
                         'interfacename '
                         'from {} '
                         'where timestamp > {} '
                         'ALLOW FILTERING').format(MODEM, span)
    spec_query[GPS] = ('SELECT nodeid, timestamp '
                       'from {} '
                       'where timestamp > {} '
                       'ALLOW FILTERING').format(GPS, span)

    # Existance checks
    nodes_seen = set()
    for table_name, query in simple_query.iteritems():
        try:
            for row in session.execute(query):
                nodes_seen.add(int(row['nodeid']))
        except Exception as e:
            print "Error for table {} : {}".format(table_name, e)

    nodes = {}
    for node in nodes_seen:
        nodes[node] = {}

    # Error check
    for table_name, query in error_query.iteritems():
        try:
            for row in session.execute(query):
                nodes[int(row['nodeid'])]['Watchdog.failed'] = True
        except Exception as e:
            print "Error for table {} : {}".format(table_name, e)

    # Specific checks
    GRACE = 100000
    for table_name, query in spec_query.iteritems():
        try:
            for row in session.execute(query):
                nodeid = int(row['nodeid'])
                ts = float(row['timestamp'])
                if nodeid not in nodes:
                    nodes[nodeid] = {}

                if table_name == GPS:
                    if table_name not in nodes[nodeid]:
                        nodes[nodeid][table_name] = {'timestamp': ts}

                    if abs(nodes[nodeid][table_name]['timestamp'] - ts) < GRACE:
                        nodes[nodeid][table_name]['timestamp'] = ts

                if table_name == PING or table_name == MODEM:
                    iccid = long(row['iccid'])
                    op = str(row['operator'])
                    if table_name not in nodes[nodeid]:
                        nodes[nodeid][table_name] = {}

                    if (iccid not in nodes[nodeid][table_name] or
                        abs(nodes[nodeid][table_name][iccid]['timestamp'] -
                        ts) < GRACE):
                        nodes[nodeid][table_name][iccid] = {
                                                            'operator': op,
                                                            'timestamp': ts
                                                            }

        except Exception as e:
            print "Error for table {} : {}".format(table_name, e)

    print "| {: <6} | {: <3} | {: <30} | {: <30} |".format('NodeID',
                                             'GPS',
                                             'Ping(operator)',
                                             'Modem(operator)')
    print "| {:-<6} | {:-<3} | {:-<30} | {:-<30} |".format("", "", "", "")


    nodeline = ""

    for node in nodes:
        gpsstr = "-"
        if (GPS in nodes[node] and
            abs(now - nodes[node][GPS]['timestamp']) < GRACE):
            gpsstr = "X"

        pingstr = "{} ({})".format(0, "-,-,-")
        if PING in nodes[node]:
            operators = []
            for iccid in nodes[node][PING]:
                diff = abs(now - nodes[node][PING][iccid]['timestamp'])
                op = nodes[node][PING][iccid]['operator']
                if diff < GRACE:
                    operators.append("{}".format(str(op)))

            nrop = len(operators)
            while len(operators) < 3:
                operators.append("-")
            pingstr = "{} ({})".format(nrop, ",".join(operators))

        modemstr = "{} ({})".format(0, "-,-,-")
        if MODEM in nodes[node]:
            operators = []
            for iccid in nodes[node][MODEM]:
                diff = abs(now - nodes[node][MODEM][iccid]['timestamp'])
                op = nodes[node][MODEM][iccid]['operator']
                if diff < GRACE:
                    operators.append("{}".format(str(op)))

            nrop = len(operators)
            while len(operators) < 3:
                operators.append("-")
            modemstr = "{} ({})".format(nrop, ",".join(operators))

        print "| {: <6} | {: <3} | {: <30} | {: <30} |".format(node,
                                                               gpsstr,
                                                               pingstr,
                                                               modemstr)

#        print "Nodes in table {} : {}".format(table_name, ",".join([str(e) for e in nodes]))

    cluster.shutdown()
