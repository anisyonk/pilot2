#!/usr/bin/env python
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
#
# Authors:
# - Tobias Wegner, tobias.wegner@cern.ch, 2017-2018
# - Paul Nilsson, paul.nilsson@cern.ch, 2017

# Reimplemented by Alexey Anisenkov

import os
import logging
import re
from time import time

from .common import resolve_common_transfer_errors, verify_catalog_checksum
from pilot.util.container import execute
from pilot.common.exception import PilotException, ErrorCodes

logger = logging.getLogger(__name__)

require_replicas = True  ## indicate if given copytool requires input replicas to be resolved
allowed_schemas = ['root']  # prioritized list of supported schemas for transfers by given copytool

copy_command = 'xrdcp'


def is_valid_for_copy_in(files):
    return True  ## FIX ME LATER


def is_valid_for_copy_out(files):
    return True  ## FIX ME LATER


def get_timeout(filesize):   ## ISOLATE ME LATER
    """ Get a proper time-out limit based on the file size """

    timeout_max = 3 * 3600  # 3 hours
    timeout_min = 300  # self.timeout

    timeout = timeout_min + int(filesize / 0.5e6)  # approx < 0.5 Mb/sec

    return min(timeout, timeout_max)


def _resolve_checksum_option(setup, **kwargs):

    cmd = "%s --version" % copy_command
    if setup:
        cmd = "source %s; %s" % (setup, cmd)

    logger.info("Execute command (%s) to check xrdcp client version" % cmd)

    rcode, stdout, stderr = execute(cmd, **kwargs)
    logger.info("return code: %s" % rcode)
    logger.info("return output: %s" % (stdout + stderr))

    cmd = "%s -h" % copy_command
    if setup:
        cmd = "source %s; %s" % (setup, cmd)

    logger.info("Execute command (%s) to decide which option should be used to calc/verify file checksum.." % cmd)

    rcode, stdout, stderr = execute(cmd, **kwargs)
    output = stdout + stderr
    logger.info("return code: %s" % rcode)
    logger.debug("return output: %s" % output)

    coption = ""
    checksum_type = 'adler32'  ## consider only adler32 for now

    if rcode:
        logger.error('FAILED to execute command=%s: %s' % (cmd, output))
    else:
        if "--cksum" in output:
            coption = "--cksum %s:print" % checksum_type
        elif "-adler" in output and checksum_type == 'adler32':
            coption = "-adler"
        elif "-md5" in output and checksum_type == 'md5':
            coption = "-md5"

    if coption:
        logger.info("Use %s option to get the checksum for %s command" % (coption, copy_command))

    return coption


def _stagefile(coption, source, destination, filesize, is_stagein, setup=None, **kwargs):
    """
        Stage the file (stagein or stageout)
        :return: destination file details (checksum, checksum_type) in case of success, throw exception in case of failure
        :raise: PilotException in case of controlled error
    """

    cmd = '%s -np -f %s %s %s' % (copy_command, coption, source, destination)
    if setup:
        cmd = "source %s; %s" % (setup, cmd)

    #timeout = get_timeout(filesize)
    #logger.info("Executing command: %s, timeout=%s" % (cmd, timeout))

    rcode, stdout, stderr = execute(cmd, **kwargs)
    logger.info('rcode=%d, stdout=%s, stderr=%s' % (rcode, stdout, stderr))

    if rcode:  ## error occurred
        error = resolve_common_transfer_errors(stdout + stderr, is_stagein=is_stagein)

        #rcode = error.get('rcode')  ## TO BE IMPLEMENTED
        #if not is_stagein and rcode == PilotErrors.ERR_CHKSUMNOTSUP: ## stage-out, on fly checksum verification is not supported .. ignore
        #    logger.info('stage-out: ignore ERR_CHKSUMNOTSUP error .. will explicitly verify uploaded file')
        #    return None, None

        raise PilotException(error.get('error'), code=error.get('rcode'), state=error.get('state'))

    # extract filesize and checksum values from output
    if coption != "":
        filesize_output, checksum_output, checksum_type = get_file_info_from_output(stdout)

    ## verify transfer by returned checksum or call remote checksum calculation
    ## to be moved at the base level

    is_verified = True   ## TO BE IMPLEMENTED LATER

    if not is_verified:
        rcode = ErrorCodes.GETADMISMATCH if is_stagein else ErrorCodes.PUTADMISMATCH
        raise PilotException("Copy command failed", code=rcode, state='AD_MISMATCH')


def copy_in(files, **kwargs):
    """
        Download given files using xrdcp command.

        :param files: list of `FileSpec` objects
        :raise: PilotException in case of controlled error
    """

    allow_direct_access = kwargs.get('allow_direct_access') or False
    setup = kwargs.pop('copytools', {}).get('xrdcp', {}).get('setup')
    coption = _resolve_checksum_option(setup, **kwargs)
    trace_report = kwargs.get('trace_report')

    localsite = os.environ.get('DQ2_LOCAL_SITE_ID', None)
    for fspec in files:
        # update the trace report
        localsite = localsite if localsite else fspec.ddmendpoint
        trace_report.update(localSite=localsite, remoteSite=fspec.ddmendpoint, filesize=fspec.filesize)
        trace_report.update(filename=fspec.lfn, guid=fspec.guid.replace('-', ''))
        trace_report.update(scope=fspec.scope, dataset=fspec.dataset)

        # continue loop for files that are to be accessed directly
        if fspec.is_directaccess(ensure_replica=False) and allow_direct_access:
            fspec.status_code = 0
            fspec.status = 'remote_io'
            trace_report.update(url=fspec.turl, clientState='FOUND_ROOT', stateReason='direct_access')
            trace_report.send()
            continue

        trace_report.update(catStart=time())

        dst = fspec.workdir or kwargs.get('workdir') or '.'
        destination = os.path.join(dst, fspec.lfn)
        try:
            _stagefile(coption, fspec.turl, destination, fspec.filesize, is_stagein=True, setup=setup, **kwargs)
            fspec.status_code = 0
            fspec.status = 'transferred'
        except Exception as error:
            fspec.status = 'failed'
            fspec.status_code = error.get_error_code() if isinstance(error, PilotException) else ErrorCodes.STAGEINFAILED
            diagnostics = error.get_detail() if isinstance(error, PilotException) else "(consult log)"
            state = 'STAGEIN_ATTEMPT_FAILED'
            trace_report.update(clientState=state, stateReason=diagnostics, timeEnd=time())
            trace_report.send()
            raise PilotException(diagnostics, code=fspec.status_code, state=state)

        trace_report.update(clientState='DONE', stateReason='OK', timeEnd=time())
        trace_report.send()

    return files


def copy_out(files, **kwargs):
    """
        Upload given files using xrdcp command.

        :param files: list of `FileSpec` objects
        :raise: PilotException in case of controlled error
    """

    setup = kwargs.pop('copytools', {}).get('xrdcp', {}).get('setup')
    coption = _resolve_checksum_option(setup, **kwargs)
    trace_report = kwargs.get('trace_report')

    for fspec in files:
        trace_report.update(scope=fspec.scope, dataset=fspec.dataset, url=fspec.surl, filesize=fspec.filesize)
        trace_report.update(catStart=time(), filename=fspec.lfn, guid=fspec.guid.replace('-', ''))

        try:
            _stagefile(coption, fspec.surl, fspec.turl, fspec.filesize, is_stagein=False, setup=setup, **kwargs)
            fspec.status_code = 0
            fspec.status = 'transferred'
            trace_report.update(clientState='DONE', stateReason='OK', timeEnd=time())
            trace_report.send()
        except PilotException as error:
            fspec.status = 'failed'
            fspec.status_code = error.get_error_code() if isinstance(error, PilotException) else ErrorCodes.STAGEOUTFAILED
            trace_report.update(clientState=error.get('state', None) or 'STAGEOUT_ATTEMPT_FAILED',
                                stateReason=error.get('error', 'unknown error'),
                                timeEnd=time())
            trace_report.send()
            raise

    return files


def get_file_info_from_output(output):
    """
    Extract file size, checksum value from xrdcp --chksum command output

    :return: (filesize [int/None], checksum, checksum_type) or (None, None, None) in case of failure
    """

    if not output:
        return None, None

    if not ("xrootd" in output or "XRootD" in output or "adler32" in output):
        logger.warning("WARNING: Failed to extract checksum: Unexpected output: %s" % output)
        return None, None

    pattern = "(?P<type>md5|adler32):\ (?P<checksum>[a-zA-Z0-9]+)\ \S+\ (?P<filesize>[0-9]+)"
    filesize, checksum, checksum_type = None, None, None

    m = re.search(pattern, output)
    if m:
        checksum_type = m.group('type')
        checksum = m.group('checksum')
        checksum = checksum.zfill(8)  # make it 8 chars length (adler32 xrdcp fix)
        filesize = m.group('filesize')
        if filesize:
            try:
                filesize = int(filesize)
            except ValueError as e:
                logger.warning('failed to convert filesize to int: %s' % e)
                filesize = None
    else:
        logger.warning("WARNING: Checksum/file size info not found in output: failed to match pattern=%s in output=%s" % (pattern, output))

    return filesize, checksum, checksum_type
