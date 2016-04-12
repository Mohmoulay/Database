#!/usr/bin/python
# -*- coding: utf-8 -*-

# Author: Jonas Karlsson
# Date: April 2016
# License: GNU General Public License v3
# Developed for use by the EU H2020 MONROE project

r"""
Very simple data importer that inserts json objects into a Cassandra db.

All files in, the specified directory and all subdirectories excluding
failed and done directories, containing
json objects are read and parsed into CQL (Cassandra) statements.

Note: A json object must end with a newline \n (and should for performance
 be on a single line).
 The program is (read tries to be) designed after http://tinyurl.com/q82wtpc
"""
import json
import time
import os
import sys
from glob import iglob
import argparse
import textwrap
import syslog
from multiprocessing.pool import ThreadPool, cpu_count
import fnmatch

from cassandra.cluster import Cluster
from cassandra.query import BatchStatement
from cassandra.query import SimpleStatement
from cassandra.query import dict_factory
from cassandra import ConsistencyLevel
from cassandra import InvalidRequest
from cassandra.auth import PlainTextAuthProvider

CMD_NAME = os.path.basename(__file__)


def get_cql(j, tablename):
    """Create the CQL INSERT statement string."""
    # Flatten message
    columns = j

    headers = ','.join(columns.keys())
    values_placeholder = ','.join('%s' for v in columns.values())
    cql_string = "INSERT INTO {} ({}) VALUES ({})".format(tablename, headers,
                                                          values_placeholder)

    return (cql_string, columns.values())


def parse_json(f):
    """
    Parse JSON objects from open file f.

    Several objects may be present in the file, and an object may be spread
    across several lines. Two objects may not occupy the same line.
    """
    jsons = []
    for line in f:
        # This while loops allow JSON objects to be pretty printed in the files
        # WARNING: A single corrupt JSON object invalidates the entire file.
        # RATIONALE: To ease debug/eror tracking
        # (ie do not modify original faulty file)
        # FIXME: If it becomes a performance problem

        each_json_on_single_line = True
        while True:
            # Try to build JSON (will fail if the object is not complete)
            # One could use a {} pattern matching algorithm for avoiding
            # try/catch however this is "hairy" as JSON allows {} inside
            # strings, see comment by Petr Viktorin http://tinyurl.com/gvwq7cy
            try:
                jsons.append(json.loads(line))
                break
            except ValueError:
                # Not yet a complete JSON value add next line and try again
                try:
                    line += next(f)
                    each_json_on_single_line = False
                except StopIteration as error:
                    # End of file without complete JSON object; probably a
                    # malformed file, discard entire file for now
                    raise Exception("Parse Error {}".format(error))

    if (not each_json_on_single_line):
        syslog.syslog(syslog.LOG_WARNING,
                      ("possible performance hit : file {} contains "
                       "pretty printed JSON objects").format(f.name))
    return jsons


def create_batch(json_store, print_stm=False):
    """
    Create Cassandra batch from JSONs in json_store.

    If true print_stm will cause the stamenets to be printed on stdout
    """
    inserts = 0

    batch = BatchStatement(consistency_level=ConsistencyLevel.ONE)
    for j in json_store:
        tablename = j['DataId'].replace('.', '_')
        stm_str, values = get_cql(j, tablename)
        if (print_stm):
            print("Statement: {} ({})".format(
                                        stm_str,
                                        ','.join(str(v) for v in values)))

        stm = SimpleStatement(stm_str)
        batch.add(stm, values)
        inserts += 1

    return (inserts, batch)


def handle_file(filename, failed_dir, processed_dir, session):
    """
    Parse and insert file in db.

    Parse the file and tries to insert it into the database.
    move finished files to failed_dir and sucsseful to processed_dir.
    """
    try:
        json_store = []

        # Sanity Check 1: Zero files size and existance check
        if os.stat(filename).st_size == 0:
            raise Exception("Zero file size")

        # Read and parse file
        with open(filename, 'r') as f:
            json_store.extend(parse_json(f))

        inserts, batch = create_batch(
            json_store,
            session is None)

        if (session):
            session.execute(batch)
        else:
            print("Executed bath on file {}".format(filename))

        # Success: Move the file we have already added to the database
        dest_path = processed_dir + os.path.basename(filename)
        if (session):
            os.rename(filename, dest_path)
            if (session is None):
                sys.stdout.write('.')
        return inserts

    # Fail: We could not parse the file or insert it into the database
    except Exception as error:
        dest_path = failed_dir + os.path.basename(filename)
        log_str = "{} in file {} moving to {}".format(
            error,
            filename,
            dest_path)

        if (session):
            os.rename(filename, dest_path)
            syslog.syslog(syslog.LOG_ERR, log_str)
        else:
            print log_str
        return 0


def schedule_workers(in_dir,
                     failed_dir,
                     processed_dir,
                     concurrency,
                     session):
    """Traverse the directory tree and kick off workers to handle the files."""
    file_count = 0
    pool = ThreadPool(processes=concurrency)
    async_results = []
    # Scan in_dir and look for all files ending in .json excluding
    # processsed_dir and failed_dir to avoid insert "loops"
    # For performance failed and processed dir should (probably)
    # not be placed below in_dir in the directory tree
    exclude = set([processed_dir, failed_dir])
    for root, dirs, files in os.walk(in_dir, topdown=True):
        # Exclude the failed and processed dirs by doing in place manipulating
        # of the returned directory list. ONLY works with topdown=True!
        dirs[:] = [d for d in dirs if os.path.join(in_dir, d) not in exclude]

        for filename in fnmatch.filter(files, '*.json'):
            path = os.path.join(root, filename)
            file_count += 1
            result = pool.apply_async(handle_file,
                                      (path,
                                       failed_dir,
                                       processed_dir,
                                       session,))
            async_results.append(result)

    pool.close()
    pool.join()
    results = [async_result.get() for async_result in async_results]
    insert_count = sum(results)
    failed_count = len([e for e in results if e == 0])
    return (file_count, insert_count, failed_count)


def parse_files(session,
                interval,
                in_dir,
                failed_dir,
                processed_dir,
                concurrency):
    """Scan in_dir for files."""
    while True:
        start_time = time.time()

        print('Start parsing files.')
        file_count, insert_count, failed_count = schedule_workers(
                                                    in_dir,
                                                    failed_dir,
                                                    processed_dir,
                                                    concurrency,
                                                    session)

        # Calculate time we should wait to satisfy the interval requirement
        elapsed = time.time() - start_time
        log_str = ("Parsing {} files and doing {} inserts took {} s, {} "
                   " files failed").format(file_count,
                                           insert_count,
                                           elapsed,
                                           failed_count)
        if (session):
            syslog.syslog(log_str)
        print log_str
        # Wait if interval > 0 else finish loop and return
        if (interval > 0):
            wait = interval - elapsed if (interval - elapsed > 0) else 0
            log_str = "Now waiting {} s before next run".format(wait)
            if (session):
                syslog.syslog(log_str)
            print log_str
            time.sleep(wait)
        else:
            break


def create_arg_parser():
    """Create a argument parser and return it."""
    max_concurrency = cpu_count()
    parser = argparse.ArgumentParser(
        prog=CMD_NAME,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent('''
            Parses .json files in in_dir and inserts them into Cassandra
            Cluster specified in -H/--hosts.
            All directories must exist otherwise before start or unforseen
            consequencs will happen.
            '''))
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
    parser.add_argument('-i', '--interval',
                        metavar='N',
                        type=int,
                        default=-1,
                        help="Seconds between scans (default -1, run once)")
    parser.add_argument('-c', '--concurrency',
                        metavar='N',
                        default=1,
                        type=int,
                        choices=xrange(1, max_concurrency),
                        help=("number of cores to utilize ("
                              "default 1, "
                              "max={})").format(max_concurrency - 1))
    parser.add_argument('-I', '--indir',
                        metavar='DIR',
                        default="/indir",
                        help="Directory to scan (default /indir/)")
    parser.add_argument('-F', '--failed',
                        metavar='DIR',
                        help="Failed files (default --indir + /failed/)")
    parser.add_argument('-P', '--processed',
                        metavar='DIR',
                        help="Processed files (default --indir + /processed/)")
    parser.add_argument('--debug',
                        action="store_true",
                        help="Do not execute queries or move files")
    parser.add_argument('--authenv',
                        action="store_true",
                        help=("Use environment variables MONROE_DB_USER and "
                              "MONROE_DB_PASSWD as username and password"))
    parser.add_argument('-v', '--version',
                        action="version",
                        version="%(prog)s 1.0")
    return parser


def parse_special_args(args, parser):
    """Parse and varifies user,password and failed,processed dirs."""
    db_user = None
    db_password = None
    failed_dir = None
    processed_dir = None
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

    # Default values of failed and processed dirs i dependent on args.indir
    if args.failed:
        failed_dir = args.failed
    else:
        failed_dir = args.indir + "/failed/"

    if args.processed:
        processed_dir = args.processed
    else:
        processed_dir = args.indir + "/processed/"

    return (db_user, db_password, failed_dir, processed_dir)

if __name__ == '__main__':
    parser = create_arg_parser()
    args = parser.parse_args()

    db_user, db_password, failed_dir, processed_dir = parse_special_args(
        args,
        parser)

    # Assuming default port: 9042, clusters and sessions are longlived and
    # should be reused
    session = None
    cluster = None
    if not args.debug:
        auth = PlainTextAuthProvider(username=db_user, password=db_password)
        cluster = Cluster(args.hosts, auth_provider=auth)
        session = cluster.connect(args.keyspace)
        session.row_factory = dict_factory
    else:
        print("Debug mode: will not insert any posts or move any files\n"
              "Info and Statements are printed to stdout\n"
              "{} called with variables \nuser={} \npassword={} \nhost={} "
              "\nkeyspace={} \nindir={} \nfaileddir={} \nprocessedir={} "
              "\ninterval={} \nConcurrency={}").format(CMD_NAME,
                                                       db_user,
                                                       db_password,
                                                       args.hosts,
                                                       args.keyspace,
                                                       args.indir,
                                                       failed_dir,
                                                       processed_dir,
                                                       args.interval,
                                                       args.concurrency)

    parse_files(session,
                args.interval,
                args.indir,
                failed_dir,
                processed_dir,
                args.concurrency)

    if not args.debug:
        cluster.shutdown()