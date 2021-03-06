#!/usr/bin/env python
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
#
# Authors:
# - Paul Nilsson, paul.nilsson@cern.ch, 2017-2019

import os
import pipes
import re
# for user container test: import urllib

from pilot.common.errorcodes import ErrorCodes
from pilot.common.exception import PilotException
from pilot.user.atlas.setup import get_asetup, get_file_system_root_path
from pilot.info import InfoService, infosys
from pilot.util.auxiliary import get_logger
from pilot.util.config import config
from pilot.util.filehandling import write_file

import logging
logger = logging.getLogger(__name__)

errors = ErrorCodes()


def do_use_container(**kwargs):
    """
    Decide whether to use a container or not.

    :param kwargs: dictionary of key-word arguments.
    :return: True if function has decided that a container should be used, False otherwise (boolean).
    """

    # to force no container use: return False
    use_container = False

    job = kwargs.get('job', False)
    copytool = kwargs.get('copytool', False)
    if job:
        # for user jobs, TRF option --containerImage must have been used, ie imagename must be set
        if job.is_analysis() and job.imagename:
            use_container = True  # False   WARNING will this change break runcontainer usage?
            logger.debug('job.is_analysis() and job.imagename -> use_container = True')
        elif not (job.platform or job.alrbuserplatform):
            use_container = False
            logger.debug('not (job.platform or job.alrbuserplatform) -> use_container = False')
        else:
            queuedata = job.infosys.queuedata
            container_name = queuedata.container_type.get("pilot")
            if container_name:
                use_container = True
                logger.debug('container_name == \'%s\' -> use_container = True' % container_name)
            else:
                logger.debug('else -> use_container = False')
    elif copytool:
        # override for copytools - use a container for stage-in/out
        use_container = True
        logger.debug('copytool -> use_container = False')
    else:
        logger.debug('not job -> use_container = False')

    return use_container


def wrapper(executable, **kwargs):
    """
    Wrapper function for any container specific usage.
    This function will be called by pilot.util.container.execute() and prepends the executable with a container command.

    :param executable: command to be executed (string).
    :param kwargs: dictionary of key-word arguments.
    :return: executable wrapped with container command (string).
    """

    workdir = kwargs.get('workdir', '.')
    pilot_home = os.environ.get('PILOT_HOME', '')
    job = kwargs.get('job', None)

    logger.info('container wrapper called')

    if workdir == '.' and pilot_home != '':
        workdir = pilot_home

    # if job.imagename (from --containerimage <image>) is set, then always use raw singularity
    if config.Container.setup_type == "ALRB":  # and job and not job.imagename:
        fctn = alrb_wrapper
    else:
        fctn = singularity_wrapper
    return fctn(executable, workdir, job=job)


def extract_platform_and_os(platform):
    """
    Extract the platform and OS substring from platform

    :param platform (string): E.g. "x86_64-slc6-gcc48-opt"
    :return: extracted platform specifics (string). E.g. "x86_64-slc6". In case of failure, return the full platform
    """

    pattern = r"([A-Za-z0-9_-]+)-.+-.+"
    a = re.findall(re.compile(pattern), platform)

    if len(a) > 0:
        ret = a[0]
    else:
        logger.warning("could not extract architecture and OS substring using pattern=%s from platform=%s"
                       "(will use %s for image name)" % (pattern, platform, platform))
        ret = platform

    return ret


def get_grid_image_for_singularity(platform):
    """
    Return the full path to the singularity grid image

    :param platform: E.g. "x86_64-slc6" (string).
    :return: full path to grid image (string).
    """

    if not platform or platform == "":
        platform = "x86_64-slc6"
        logger.warning("using default platform=%s (cmtconfig not set)" % (platform))

    arch_and_os = extract_platform_and_os(platform)
    image = arch_and_os + ".img"
    _path = os.path.join(get_file_system_root_path(), "atlas.cern.ch/repo/containers/images/singularity")
    path = os.path.join(_path, image)
    if not os.path.exists(path):
        image = 'x86_64-centos7.img'
        logger.warning('path does not exist: %s (trying with image %s instead)' % (path, image))
        path = os.path.join(_path, image)
        if not os.path.exists(path):
            logger.warning('path does not exist either: %s' % path)
            path = ""

    return path


def get_middleware_type():
    """
    Return the middleware type from the container type.
    E.g. container_type = 'singularity:pilot;docker:wrapper;middleware:container'
    get_middleware_type() -> 'container', meaning that middleware should be taken from the container. The default
    is otherwise 'workernode', i.e. middleware is assumed to be present on the worker node.

    :return: middleware_type (string)
    """

    middleware_type = ""
    container_type = infosys.queuedata.container_type

    mw = 'middleware'
    if container_type and container_type != "" and mw in container_type:
        try:
            container_names = container_type.split(';')
            for name in container_names:
                t = name.split(':')
                if mw == t[0]:
                    middleware_type = t[1]
        except Exception as e:
            logger.warning("failed to parse the container name: %s, %s" % (container_type, e))
    else:
        # logger.warning("container middleware type not specified in queuedata")
        # no middleware type was specified, assume that middleware is present on worker node
        middleware_type = "workernode"

    return middleware_type


def extract_atlas_setup(asetup):
    """
    Extract the asetup command from the full setup command.
    export ATLAS_LOCAL_ROOT_BASE=/cvmfs/atlas.cern.ch/repo/ATLASLocalRootBase;
      source ${ATLAS_LOCAL_ROOT_BASE}/user/atlasLocalSetup.sh --quiet;source $AtlasSetup/scripts/asetup.sh
    -> $AtlasSetup/scripts/asetup.sh, export ATLAS_LOCAL_ROOT_BASE=/cvmfs/atlas.cern.ch/repo/ATLASLocalRootBase; source
         ${ATLAS_LOCAL_ROOT_BASE}/user/atlasLocalSetup.sh --quiet;

    :param asetup: full asetup command (string).
    :return: extracted asetup command, cleaned up full asetup command without asetup.sh (string).
    """

    try:
        # source $AtlasSetup/scripts/asetup.sh
        atlas_setup = asetup.split(';')[-1]
        # export ATLAS_LOCAL_ROOT_BASE=/cvmfs/atlas.cern.ch/repo/ATLASLocalRootBase;
        #   source ${ATLAS_LOCAL_ROOT_BASE}/user/atlasLocalSetup.sh --quiet;
        cleaned_atlas_setup = asetup.replace(atlas_setup, '')
        atlas_setup = atlas_setup.replace('source ', '')
    except Exception as e:
        logger.debug('exception caught while extracting asetup command: %s' % e)
        atlas_setup = ''
        cleaned_atlas_setup = ''

    return atlas_setup, cleaned_atlas_setup


def extract_full_atlas_setup(cmd, atlas_setup):
    """
    Extract the full asetup (including options) from the payload setup command.
    atlas_setup is typically '$AtlasSetup/scripts/asetup.sh'.

    :param cmd: full payload setup command (string).
    :param atlas_setup: asetup command (string).
    :return: extracted full asetup command, updated full payload setup command without asetup part (string).
    """

    updated_cmds = []
    extracted_asetup = ""

    if not atlas_setup:
        return extracted_asetup, cmd

    try:
        _cmd = cmd.split(';')
        for subcmd in _cmd:
            if atlas_setup in subcmd:
                extracted_asetup = subcmd
            else:
                updated_cmds.append(subcmd)
        updated_cmd = ';'.join(updated_cmds)
    except Exception as e:
        logger.warning('exception caught while extracting full atlas setup: %s' % e)
        updated_cmd = cmd
    logger.debug('updated payload setup command: %s' % updated_cmd)

    return extracted_asetup, updated_cmd


def update_alrb_setup(cmd, use_release_setup):
    """
    Update the ALRB setup command.
    Add the ALRB_CONT_SETUPFILE in case the release setup file was created earlier (required available cvmfs).

    :param cmd: full ALRB setup command (string).
    :param use_release_setup: should the release setup file be added to the setup command? (Boolean).
    :return: updated ALRB setup command (string).
    """

    updated_cmds = []
    try:
        _cmd = cmd.split(';')
        for subcmd in _cmd:
            if subcmd.startswith('source ${ATLAS_LOCAL_ROOT_BASE}') and use_release_setup:
                updated_cmds.append('export ALRB_CONT_SETUPFILE="/srv/%s"' % config.Container.release_setup)
            updated_cmds.append(subcmd)
        updated_cmd = ';'.join(updated_cmds)
    except Exception as e:
        logger.warning('exception caught while extracting full atlas setup: %s' % e)
        updated_cmd = cmd
    logger.debug('updated ALRB command: %s' % updated_cmd)

    return updated_cmd


def update_for_user_proxy(_cmd, cmd):
    """
    Add the X509 user proxy to the container sub command string if set, and remove it from the main container command.

    :param _cmd:
    :param cmd:
    :return:
    """

    x509 = os.environ.get('X509_USER_PROXY')
    if x509 != "":
        # do not include the X509_USER_PROXY in the command the container will execute
        cmd = cmd.replace("export X509_USER_PROXY=%s;" % x509, "")
        # add it instead to the container setup command
        _cmd = "export X509_USER_PROXY=%s;" % x509 + _cmd

    return _cmd, cmd


def set_platform(job, new_mode, alrb_setup):
    """
    Set thePlatform variable and add it to the sub container command.

    :param job: job object.
    :param new_mode: tmp
    :param alrb_setup: ALRB setup (string).
    :return: updated ALRB setup (string).
    """

    if job.alrbuserplatform:
        alrb_setup += 'export thePlatform=\"%s\";' % job.alrbuserplatform
    elif job.preprocess and job.containeroptions:
        alrb_setup += 'export thePlatform=\"%s\";' % job.containeroptions.get('containerImage')
    elif job.imagename and new_mode:
        alrb_setup += 'export thePlatform=\"%s\";' % job.imagename
    elif job.platform:
        alrb_setup += 'export thePlatform=\"%s\";' % job.platform

    return alrb_setup


def get_container_options(container_options):
    """
    Get the container options from AGIS for the container execution command.
    For Raythena ES jobs, replace the -C with "" (otherwise IPC does not work, needed by yampl).

    :param container_options: container options from AGIS (string).
    :return: updated container command (string).
    """

    is_raythena = config.Payload.executor_type.lower() == 'raythena'

    opts = ''
    # Set the singularity options
    if container_options:
        # the event service payload cannot use -C/--containall since it will prevent yampl from working
        if is_raythena:
            if '-C' in container_options:
                container_options = container_options.replace('-C', '')
            if '--containall' in container_options:
                container_options = container_options.replace('--containall', '')
        if container_options:
            opts += '-e \"%s\"' % container_options
    else:
        # consider using options "-c -i -p" instead of "-C". The difference is that the latter blocks all environment
        # variables by default and the former does not
        # update: skip the -i to allow IPC, otherwise yampl won't work
        if is_raythena:
            pass
            # opts += 'export ALRB_CONT_CMDOPTS=\"$ALRB_CONT_CMDOPTS -c -i -p\";'
        else:
            opts += '-e \"-C\"'

    return opts


def alrb_wrapper(cmd, workdir, job=None):
    """
    Wrap the given command with the special ALRB setup for containers
    E.g. cmd = /bin/bash hello_world.sh
    ->
    export thePlatform="x86_64-slc6-gcc48-opt"
    export ALRB_CONT_RUNPAYLOAD="cmd'
    setupATLAS -c $thePlatform

    :param cmd (string): command to be executed in a container.
    :param workdir: (not used)
    :param job: job object.
    :return: prepended command with singularity execution command (string).
    """

    if not job:
        logger.warning('the ALRB wrapper did not get a job object - cannot proceed')
        return cmd

    log = get_logger(job.jobid)
    queuedata = job.infosys.queuedata
    new_mode = True  # remove this later

    container_name = queuedata.container_type.get("pilot")  # resolve container name for user=pilot
    if container_name:
        log.debug('cmd 1=%s' % cmd)
        # first get the full setup, which should be removed from cmd (or ALRB setup won't work)
        _asetup = get_asetup()
        # get_asetup()
        # -> export ATLAS_LOCAL_ROOT_BASE=/cvmfs/atlas.cern.ch/repo/ATLASLocalRootBase;source ${ATLAS_LOCAL_ROOT_BASE}/user/atlasLocalSetup.sh
        #     --quiet;source $AtlasSetup/scripts/asetup.sh
        log.debug('_asetup: %s' % _asetup)
        # atlas_setup = $AtlasSetup/scripts/asetup.sh
        # clean_asetup = export ATLAS_LOCAL_ROOT_BASE=/cvmfs/atlas.cern.ch/repo/ATLASLocalRootBase;source
        #                   ${ATLAS_LOCAL_ROOT_BASE}/user/atlasLocalSetup.sh --quiet;
        atlas_setup, clean_asetup = extract_atlas_setup(_asetup)
        log.debug('atlas_setup=%s' % atlas_setup)
        log.debug('clean_asetup=%s' % clean_asetup)
        full_atlas_setup = get_full_asetup(cmd, 'source ' + atlas_setup)
        log.debug('full_atlas_setup=%s' % full_atlas_setup)

        if new_mode:
            # do not include 'clean_asetup' in the container script
            cmd = cmd.replace(clean_asetup, '')
            # for stand-alone containers, do not include the full atlas setup either
            if job.imagename:
                cmd = cmd.replace(full_atlas_setup, '')
        else:
            cmd = cmd.replace(_asetup, "asetup")  # else: cmd.replace(_asetup, atlas_setup)
        log.debug('cmd 2=%s' % cmd)
        # get_asetup(asetup=False)
        # -> export ATLAS_LOCAL_ROOT_BASE=/cvmfs/atlas.cern.ch/repo/ATLASLocalRootBase;source ${ATLAS_LOCAL_ROOT_BASE}/user/atlasLocalSetup.sh --quiet;

        # get simplified ALRB setup (export)
        alrb_setup = get_asetup(alrb=True, add_if=True)
        # get_asetup(alrb=True)
        # -> export ATLAS_LOCAL_ROOT_BASE=/cvmfs/atlas.cern.ch/repo/ATLASLocalRootBase;
        # get_asetup(alrb=True, add_if=True)
        # -> if [ -z "$ATLAS_LOCAL_ROOT_BASE" ]; then export ATLAS_LOCAL_ROOT_BASE=/cvmfs/atlas.cern.ch/repo/ATLASLocalRootBase; fi;
        log.debug('initial alrb_setup: %s' % alrb_setup)

        # add user proxy if necessary (actually it should also be removed from cmd)
        alrb_setup, cmd = update_for_user_proxy(alrb_setup, cmd)

        # set the platform info
        alrb_setup = set_platform(job, new_mode, alrb_setup)

        # add the jobid to be used as an identifier for the payload running inside the container
        # it is used to identify the pid for the process to be tracked by the memory monitor
        if 'export PANDAID' not in alrb_setup:
            alrb_setup += "export PANDAID=%s;" % job.jobid
        log.debug('alrb_setup=%s' % alrb_setup)

        # add TMPDIR
        cmd = "export TMPDIR=/srv;export GFORTRAN_TMPDIR=/srv;" + cmd
        cmd = cmd.replace(';;', ';')
        log.debug('cmd = %s' % cmd)

        # get the proper release setup script name, and create the script if necessary
        release_setup, cmd = create_release_setup(cmd, atlas_setup, full_atlas_setup, job.swrelease, job.imagename,
                                                  job.workdir, queuedata.is_cvmfs, new_mode)
        if not cmd:
            diagnostics = 'payload setup was reset due to missing release setup in unpacked container'
            logger.warning(diagnostics)
            job.piloterrorcodes, job.piloterrordiags = errors.add_error_code(errors.MISSINGRELEASEUNPACKED)
            return ""

        # correct full payload command in case preprocess command are used (ie replace trf with setupATLAS -c ..)
        if job.preprocess and job.containeroptions:
            cmd = replace_last_command(cmd, job.containeroptions.get('containerExec'))
            log.debug('updated cmd with containerExec: %s' % cmd)

        # write the full payload command to a script file
        container_script = config.Container.container_script
        log.debug('command to be written to container script file:\n\n%s:\n\n%s\n' % (container_script, cmd))
        try:
            write_file(os.path.join(job.workdir, container_script), cmd, mute=False)
            os.chmod(os.path.join(job.workdir, container_script), 0o755)  # Python 2/3
        except Exception as e:
            log.warning('exception caught: %s' % e)
            return ""

        # also store the command string in the job object
        job.command = cmd

        # add atlasLocalSetup command + options (overwrite the old cmd since the new cmd is the containerised version)
        cmd = add_asetup(job, alrb_setup, queuedata.is_cvmfs, release_setup, container_script, queuedata.container_options, new_mode)
        log.debug('\n\nfinal command:\n\n%s\n' % cmd)
    else:
        log.warning('container name not defined in AGIS')

    return cmd


def is_release_setup(script, imagename):
    """
    Does the release_setup.sh file exist?
    This check can only be made for unpacked containers. These must have the release setup file present, or setup will
    fail. For non-unpacked containers, the function will return True and the pilot will assume that the container has
    the setup file.

    :param script: release setup script (string).
    :param imagename: container/image name (string).
    :return: Boolean.
    """

    if 'unpacked' in imagename:
        if script.startswith('/'):
            script = script[1:]
        exists = True if os.path.exists(os.path.join(imagename, script)) else False
        if exists:
            logger.info('%s is present in %s' % (script, imagename))
        else:
            logger.warning('%s is not present in %s - setup has failed' % (script, imagename))
    else:
        exists = True
        logger.info('%s is assumed to be present in %s' % (script, imagename))
    return exists


def add_asetup(job, alrb_setup, is_cvmfs, release_setup, container_script, container_options, new_mode):
    """
    Add atlasLocalSetup and options to form the final payload command.

    :param job: job object.
    :param alrb_setup: ALRB setup (string).
    :param is_cvmfs: True for cvmfs sites (Boolean).
    :param release_setup: release setup (string).
    :param container_script: container script name (string).
    :param container_options: container options (string).
    :param new_mode: temp (Boolean).
    :return: final payload command (string).
    """

    # this should not be necessary after the extract_container_image() in JobData update
    # containerImage should have been removed already
    if '--containerImage' in job.jobparams:
        job.jobparams, container_path = remove_container_string(job.jobparams)
        if job.alrbuserplatform:
            if not is_cvmfs:
                alrb_setup += 'source ${ATLAS_LOCAL_ROOT_BASE}/user/atlasLocalSetup.sh -c %s' % job.alrbuserplatform
        elif container_path != "":
            alrb_setup += 'source ${ATLAS_LOCAL_ROOT_BASE}/user/atlasLocalSetup.sh -c %s' % container_path
        else:
            logger.warning('failed to extract container path from %s' % job.jobparams)
            alrb_setup = ""
        if alrb_setup and not is_cvmfs:
            alrb_setup += ' -d'
    else:
        alrb_setup += 'source ${ATLAS_LOCAL_ROOT_BASE}/user/atlasLocalSetup.sh '
        if job.platform or job.alrbuserplatform or (job.imagename and new_mode):
            alrb_setup += '-c $thePlatform'
            if not is_cvmfs:
                alrb_setup += ' -d'

    # update the ALRB setup command
    # alrb_setup = update_alrb_setup(alrb_setup, new_mode and queuedata.is_cvmfs and use_release_setup)
    if new_mode:
        alrb_setup += ' -s %s' % release_setup
        alrb_setup += ' -r /srv/' + container_script
    alrb_setup = alrb_setup.replace('  ', ' ').replace(';;', ';')

    # add container options
    alrb_setup += ' ' + get_container_options(container_options)
    alrb_setup = alrb_setup.replace('  ', ' ')
    cmd = alrb_setup

    # correct full payload command in case preprocess command are used (ie replace trf with setupATLAS -c ..)
    #if job.preprocess and job.containeroptions:
    #    logger.debug('will update cmd=%s' % cmd)
    #    cmd = replace_last_command(cmd, 'source ${ATLAS_LOCAL_ROOT_BASE}/user/atlasLocalSetup.sh -c $thePlatform')
    #    logger.debug('updated cmd with containerImage')

    return cmd


def get_full_asetup(cmd, atlas_setup):
    """
    Extract the full asetup command from the payload execution command.
    (Easier that generating it again). We need to remove this command for stand-alone containers.
    Alternatively: do not include it in the first place (but this seems to trigger the need for further changes).
    atlas_setup is "source $AtlasSetup/scripts/asetup.sh", which is extracted in a previous step.
    The function typically returns: "source $AtlasSetup/scripts/asetup.sh 21.0,Athena,2020-05-19T2148,notest --makeflags='$MAKEFLAGS';".

    :param cmd: payload execution command (string).
    :param atlas_setup: extracted atlas setup (string).
    :return: full atlas setup (string).
    """

    nr = cmd.find(atlas_setup)
    cmd = cmd[nr:]  # remove everything before 'source $AtlasSetup/..'
    nr = cmd.find(';')
    cmd = cmd[:nr + 1]  # remove everything after the first ;, but include the trailing ;

    return cmd


def replace_last_command(cmd, replacement):
    """
    Replace the last command in cmd with given replacement.

    :param cmd: command (string).
    :param replacement: replacement (string).
    :return: updated command (string).
    """

    cmd = cmd.strip('; ')
    last_bit = cmd.split(';')[-1]
    cmd = cmd.replace(last_bit.strip(), replacement)

    return cmd


def create_release_setup(cmd, atlas_setup, full_atlas_setup, release, imagename, workdir, is_cvmfs, new_mode):
    """
    Get the proper release setup script name, and create the script if necessary.

    This function also updates the cmd string (removes full asetup from payload command).

    Note: for stand-alone containers, the function will return /release_setup.sh and assume that this script exists
    in the container. The pilot will only create a my_release_setup.sh script for OS containers.

    In case the release setup is not present in an unpacked container, the function will reset the cmd string.

    :param cmd: Payload execution command (string).
    :param atlas_setup: asetup command (string).
    :param full_atlas_setup: full asetup command (string).
    :param release: software release, needed to determine Athena environment (string).
    :param imagename: container image name (string).
    :param workdir: job workdir (string).
    :param is_cvmfs: does the queue have cvmfs? (Boolean).
    :param new_mode: temporary new_mode for new ALRB setup (REMOVE).
    :return: proper release setup name (string), updated cmd (string).
    """

    release_setup_name = get_release_setup_name(release, imagename)

    # note: if release_setup_name.startswith('/'), the pilot will NOT create the script
    if new_mode and not release_setup_name.startswith('/'):
        # in the new mode, extracted_asetup should be written to 'my_release_setup.sh' and cmd to 'container_script.sh'
        content = ''
        if is_cvmfs:
            content, cmd = extract_full_atlas_setup(cmd, atlas_setup)
            if not content:
                content = full_atlas_setup
        if not content:
            content = 'echo \"Error: this setup file should not be run since %s should exist inside the container\"' % release_setup_name
            logger.debug(
                'will create an empty (almost) release setup file since asetup could not be extracted from command')
        #else:
        #    _content = "\echo \"This will not be run if there is a /release_setup.sh script inside the container\"\n\n"
        #    _content += "if [ -z $AtlasSetup ]; then\n"
        #    _content += "    \echo \"asetup not found so will try to do it from cvmfs ...\"\n"
        #    _content += "    if [ -z \"$ATLAS_LOCAL_ROOT_BASE\" ]; then\n"
        #    _content += "        export ATLAS_LOCAL_ROOT_BASE=%s/atlas.cern.ch/repo/ATLASLocalRootBase\n" % get_file_system_root_path()
        #    _content += "    fi\n"
        #    _content += "    source $ATLAS_LOCAL_ROOT_BASE/user/atlasLocalSetup.sh -q\n"
        #    _content += "fi\n\n%s" % content
        #    content = _content
        logger.debug('command to be written to release setup file:\n\n%s:\n\n%s\n' % (release_setup_name, content))
        try:
            write_file(os.path.join(workdir, release_setup_name), content, mute=False)
        except Exception as e:
            logger.warning('exception caught: %s' % e)
    else:
        # reset cmd in case release_setup.sh does not exist in unpacked image (only for those containers)
        cmd = cmd.replace(';;', ';') if is_release_setup(release_setup_name, imagename) else ''

    # add the /srv for OS containers
    if not release_setup_name.startswith('/'):
        release_setup_name = os.path.join('/srv', release_setup_name)

    return release_setup_name, cmd


def get_release_setup_name(release, imagename):
    """
    Return the file name for the release setup script.

    NOTE: the /srv path will only be added later, in the case of OS containers.

    For OS containers, return config.Container.release_setup (my_release_setup.sh);
    for stand-alone containers (user defined containers, ie when --containerImage or job.imagename was used/set),
    return '/release_setup.sh'. my_release_setup.sh will NOT be created for stand-alone containers.
    The pilot will specify /release_setup.sh only when jobs use the Athena environment (ie has a set job.swrelease).

    :param release: software release (string).
    :param imagename: container image name (string).
    :return: release setup file name (string).
    """

    if imagename and release and release != 'NULL':
        # stand-alone containers (script is assumed to exist inside image/container)
        release_setup_name = '/release_setup.sh'
    else:
        # OS containers (script will be created by pilot)
        release_setup_name = config.Container.release_setup
        if not release_setup_name:
            release_setup_name = 'my_release_setup.sh'

    # note: if release_setup_name.startswith('/'), the pilot will NOT create the script

    return release_setup_name


## DEPRECATED, remove after verification with user container job
def remove_container_string(job_params):
    """ Retrieve the container string from the job parameters """

    pattern = r" \'?\-\-containerImage\=?\ ?([\S]+)\ ?\'?"
    compiled_pattern = re.compile(pattern)

    # remove any present ' around the option as well
    job_params = re.sub(r'\'\ \'', ' ', job_params)

    # extract the container path
    found = re.findall(compiled_pattern, job_params)
    container_path = found[0] if len(found) > 0 else ""

    # Remove the pattern and update the job parameters
    job_params = re.sub(pattern, ' ', job_params)

    return job_params, container_path


def singularity_wrapper(cmd, workdir, job=None):
    """
    Prepend the given command with the singularity execution command
    E.g. cmd = /bin/bash hello_world.sh
    -> singularity_command = singularity exec -B <bindmountsfromcatchall> <img> /bin/bash hello_world.sh
    singularity exec -B <bindmountsfromcatchall>  /cvmfs/atlas.cern.ch/repo/images/singularity/x86_64-slc6.img <script>
    Note: if the job object is not set, then it is assumed that the middleware container is to be used.

    :param cmd: command to be prepended (string).
    :param workdir: explicit work directory where the command should be executed (needs to be set for Singularity) (string).
    :param job: job object.
    :return: prepended command with singularity execution command (string).
    """

    if job:
        queuedata = job.infosys.queuedata
    else:
        infoservice = InfoService()
        infoservice.init(os.environ.get('PILOT_SITENAME'), infosys.confinfo, infosys.extinfo)
        queuedata = infoservice.queuedata

    container_name = queuedata.container_type.get("pilot")  # resolve container name for user=pilot
    logger.debug("resolved container_name from queuedata.contaner_type: %s" % container_name)

    if container_name == 'singularity':
        logger.info("singularity has been requested")

        # Get the singularity options
        singularity_options = queuedata.container_options
        if singularity_options != "":
            singularity_options += ","
        else:
            singularity_options = "-B "
        singularity_options += "/cvmfs,${workdir},/home"
        logger.debug("using singularity_options: %s" % singularity_options)

        # Get the image path
        if job:
            image_path = job.imagename or get_grid_image_for_singularity(job.platform)
        else:
            image_path = config.Container.middleware_container

        # Does the image exist?
        if image_path:
            # Prepend it to the given command
            cmd = "export workdir=" + workdir + "; singularity --verbose exec " + singularity_options + " " + image_path + \
                  " /bin/bash -c " + pipes.quote("cd $workdir;pwd;%s" % cmd)

            # for testing user containers
            # singularity_options = "-B $PWD:/data --pwd / "
            # singularity_cmd = "singularity exec " + singularity_options + image_path
            # cmd = re.sub(r'-p "([A-Za-z0-9.%/]+)"', r'-p "%s\1"' % urllib.pathname2url(singularity_cmd), cmd)
        else:
            logger.warning("singularity options found but image does not exist")

        logger.info("updated command: %s" % cmd)

    return cmd


def create_stagein_container_command(workdir, cmd):
    """
    Create the stage-in container command.

    The function takes the isolated stage-in command, adds bits and pieces needed for the containerisation and stores
    it in a stagein.sh script file. It then generates the actual command that will execute the stage-in script in a
    container.

    new cmd:
      lsetup rucio davis xrootd
      old cmd
      exit $?
    write new cmd to stagein.sh script
    create container command and return it

    :param workdir: working directory where script will be stored (string).
    :param cmd: isolated stage-in command (string).
    :return: container command to be executed (string).
    """

    command = 'cd %s;' % workdir

    # add bits and pieces for the containerisation
    content = 'lsetup rucio davix xrootd\n%s\nexit $?' % cmd
    logger.debug('setup.sh content:\n%s' % content)

    # store it in setup.sh
    script_name = 'stagein.sh'
    try:
        status = write_file(os.path.join(workdir, script_name), content)
    except PilotException as e:
        raise e
    else:
        if status:
            # generate the final container command
            x509 = os.environ.get('X509_USER_PROXY', '')
            if x509:
                command += 'export X509_USER_PROXY=%s;' % x509
            pythonpath = 'export PYTHONPATH=%s:$PYTHONPATH;' % os.path.join(workdir, 'pilot2')
            #pythonpath = 'export PYTHONPATH=/cvmfs/atlas.cern.ch/repo/sw/PandaPilot/pilot2/latest:$PYTHONPATH;'
            command += 'export ALRB_CONT_RUNPAYLOAD=\"%ssource /srv/%s\";' % (pythonpath, script_name)
            command += get_asetup(alrb=True)  # export ATLAS_LOCAL_ROOT_BASE=/cvmfs/atlas.cern.ch/repo/ATLASLocalRootBase;
            command += 'source ${ATLAS_LOCAL_ROOT_BASE}/user/atlasLocalSetup.sh -c centos7'

    logger.debug('container command: %s' % command)
    return command
