#!/usr/bin/env python
import argparse
import os
import re

from pilot.api.data import StageInClient
from pilot.api.es_data import StageInESClient
from pilot.info import InfoService, FileSpec, infosys
from pilot.util.config import config
from pilot.util.filehandling import establish_logging, write_json
from pilot.util.tracereport import TraceReport

import logging

# error codes
GENERAL_ERROR = 1
NO_QUEUENAME = 2
NO_SCOPES = 3
NO_LFNS = 4
NO_EVENTTYPE = 5
NO_LOCALSITE = 6
NO_REMOTESITE = 7
NO_PRODUSERID = 8
NO_JOBID = 9
NO_TASKID = 10
NO_JOBDEFINITIONID = 11
TRANSFER_ERROR = 12


def get_args():
    """
    Return the args from the arg parser.

    :return: args (arg parser object).
    """

    arg_parser = argparse.ArgumentParser()

    arg_parser.add_argument('-d',
                            dest='debug',
                            action='store_true',
                            default=False,
                            help='Enable debug mode for logging messages')
    arg_parser.add_argument('-q',
                            dest='queuename',
                            required=True,
                            help='Queue name (e.g., AGLT2_TEST-condor')
    arg_parser.add_argument('-w',
                            dest='workdir',
                            required=False,
                            default=os.getcwd(),
                            help='Working directory')
    arg_parser.add_argument('--scopes',
                            dest='scopes',
                            required=True,
                            help='List of Rucio scopes (e.g., mc16_13TeV,mc16_13TeV')
    arg_parser.add_argument('--lfns',
                            dest='lfns',
                            required=True,
                            help='LFN list (e.g., filename1,filename2')
    arg_parser.add_argument('--eventtype',
                            dest='eventtype',
                            required=True,
                            help='Event type')
    arg_parser.add_argument('--localsite',
                            dest='localsite',
                            required=True,
                            help='Local site')
    arg_parser.add_argument('--remotesite',
                            dest='remotesite',
                            required=True,
                            help='Remote site')
    arg_parser.add_argument('--produserid',
                            dest='produserid',
                            required=True,
                            help='produserid')
    arg_parser.add_argument('--jobid',
                            dest='jobid',
                            required=True,
                            help='PanDA job id')
    arg_parser.add_argument('--taskid',
                            dest='taskid',
                            required=True,
                            help='PanDA task id')
    arg_parser.add_argument('--jobdefinitionid',
                            dest='jobdefinitionid',
                            required=True,
                            help='Job definition id')
    arg_parser.add_argument('--eventservicemerge',
                            dest='eventservicemerge',
                            type=str2bool,
                            default=False,
                            help='Event service merge boolean')
    arg_parser.add_argument('--usepcache',
                            dest='usepcache',
                            type=str2bool,
                            default=False,
                            help='pcache boolean from queuedata')
    arg_parser.add_argument('--no-pilot-log',
                            dest='nopilotlog',
                            action='store_true',
                            default=False,
                            help='Do not write the pilot log to file')

    return arg_parser.parse_args()


def str2bool(v):
    """ Helper function to convert string to bool """

    if isinstance(v, bool):
        return v
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')


def verify_args():
    """
    Make sure required arguments are set, and if they are not then set them.
    (deprecated)
    :return:
    """
    if not args.workdir:
        args.workdir = os.getcwd()

    if not args.queuename:
        message('queue name not set, cannot initialize InfoService')
        return NO_QUEUENAME

    if not args.scopes:
        message('scopes not set')
        return NO_SCOPES

    if not args.lfns:
        message('LFNs not set')
        return NO_LFNS

    if not args.eventtype:
        message('No event type provided')
        return NO_EVENTTYPE

    if not args.localsite:
        message('No local site provided')
        return NO_LOCALSITE

    if not args.remotesite:
        message('No remote site provided')
        return NO_REMOTESITE

    if not args.produserid:
        message('No produserid provided')
        return NO_PRODUSERID

    if not args.jobid:
        message('No jobid provided')
        return NO_JOBID

    if not args.taskid:
        message('No taskid provided')
        return NO_TASKID

    if not args.jobdefinitionid:
        message('No jobdefinitionid provided')
        return NO_JOBDEFINITIONID

    return 0


def message(msg):
    print(msg) if not logger else logger.info(msg)


def get_file_lists(lfns, scopes):
    return lfns.split(','), scopes.split(',')


class Job():
    """
    A minimal implementation of the Pilot Job class with data members necessary for the trace report only.
    """

    produserid = ""
    jobid = ""
    taskid = ""
    jobdefinitionid = ""

    def __init__(self, produserid="", jobid="", taskid="", jobdefinitionid=""):
        self.produserid = produserid.replace('%20', ' ')
        self.jobid = jobid
        self.taskid = taskid
        self.jobdefinitionid = jobdefinitionid


def add_to_dictionary(dictionary, key, value1, value2):
    """
    Add key: [value1, value2] to dictionary.

    :param dictionary: dictionary to be updated.
    :param key: key to be added (string).
    :param value1: value1 to be added to list belonging to key.
    :param value1: value2 to be added to list belonging to key.
    :return: updated dictionary.
    """

    dictionary[key] = [value1, value2]
    return dictionary


def extract_error_info(err):

    error_code = 0
    error_message = ""

    _code = re.search('error code: (\d+)', err)
    if _code:
        error_code = _code.group(1)

    _msg = re.search('details: (.+)', err)
    if _msg:
        error_message = _msg.group(1)
        error_message = error_message.replace('[PilotException(', '').strip()

    return error_code, error_message


if __name__ == '__main__':
    """
    Main function of the stage-in script.
    """

    # get the args from the arg parser
    args = get_args()
    args.debug = True
    args.nopilotlog = False

    establish_logging(args, filename=config.Pilot.stageinlog)
    logger = logging.getLogger(__name__)

    #ret = verify_args()
    #if ret:
    #    exit(ret)

    # get the file info
    lfns, scopes = get_file_lists(args.lfns, args.scopes)
    if len(lfns) != len(scopes):
        message('file lists not same length: len(lfns)=%d, len(scopes)=%d' % (len(lfns), len(scopes)))

    # generate the trace report
    trace_report = TraceReport(pq=os.environ.get('PILOT_SITENAME', ''), localSite=args.localsite, remoteSite=args.remotesite, dataset="",
                               eventType=args.eventtype)
    job = Job(produserid=args.produserid, jobid=args.jobid, taskid=args.taskid, jobdefinitionid=args.jobdefinitionid)
    trace_report.init(job)

    try:
        infoservice = InfoService()
        infoservice.init(args.queuename, infosys.confinfo, infosys.extinfo)
        infosys.init(args.queuename)  # is this correct? otherwise infosys.queuedata doesn't get set
    except Exception as e:
        message(e)

    # perform stage-in (single transfers)
    err = ""
    errcode = 0
    xfiles = None
    if args.eventservicemerge:
        client = StageInESClient(infoservice, logger=logger, trace_report=trace_report)
        activity = 'es_events_read'
    else:
        client = StageInClient(infoservice, logger=logger, trace_report=trace_report)
        activity = 'pr'
    kwargs = dict(workdir=args.workdir, cwd=args.workdir, usecontainer=False, use_pcache=args.usepcache, use_bulk=False)
    for lfn, scope in list(zip(lfns, scopes)):
        try:
            files = [{'scope': scope, 'lfn': lfn, 'workdir': args.workdir}]
            xfiles = [FileSpec(type='input', **f) for f in files]
            r = client.transfer(xfiles, activity=activity, **kwargs)
        except Exception as e:
            err = str(e)
            errcode = -1
            message(err)
            # break

    # put file statuses in a dictionary to be written to file
    file_dictionary = {}  # { 'error': [error_diag, -1], 'lfn1': [status, status_code], 'lfn2':.., .. }
    if xfiles:
        message('stagein script summary of transferred files:')
        for fspec in xfiles:
            add_to_dictionary(file_dictionary, fspec.lfn, fspec.status, fspec.status_code)
            status = fspec.status if fspec.status else "(not transferred)"
            message(" -- lfn=%s, status_code=%s, status=%s" % (fspec.lfn, fspec.status_code, status))

    # add error info, if any
    if err:
        errcode, err = extract_error_info(err)
    add_to_dictionary(file_dictionary, 'error', err, errcode)
    path = os.path.join(args.workdir, config.Container.stagein_dictionary)
    _status = write_json(path, file_dictionary)
    if err:
        message("containerised file transfers failed: %s" % err)
        exit(TRANSFER_ERROR)

    message("wrote %s" % path)
    message("containerised file transfers finished")
    exit(0)
