#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
This file is an external SSH helper for the spawner.
Its neccesary if you want to debug the spawner on your local client.
If 'AWS_SPAWNER_TEST' is set in testbenches, the spawner will import these functions
in order to route its commands to the workers via the bastion host.
"""
import logging
import json
from tornado import gen
from fabric.exceptions import NetworkError
from paramiko.ssh_exception import SSHException, ChannelException
from botocore.exceptions import ClientError, WaiterError
from concurrent.futures import ThreadPoolExecutor
from jumpssh import SSHSession, ConnectionError, RunCmdError
import os

thread_pool = ThreadPoolExecutor(100)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

class RemoteCmdExecutionError(Exception): pass
bastion_info = json.load(open('bastion_info.json', 'r'))

BASTION= bastion_info['bastion']
KEYPATH = bastion_info['key_path']
BASTIONUSER = bastion_info['user']

def _run(cmd, sudo = False, *args, **kwargs):
    '''Executes a command on a host behind a gateway. Necessary for testing 
    in development when spawning instances in private prodiction subnet. '''
    
    max_retries = kwargs.pop("max_retries", 10)
    host = os.environ.get('AWS_SPAWNER_WORKER_IP')
    gateway_session = SSHSession(BASTION, BASTIONUSER, private_key_file=KEYPATH).open()
    remote_session = gateway_session.get_remote_session(host, retry=max_retries, retry_interval=2)    
    cmd = cmd.replace('\n', '')
    if sudo:
        output = remote_session.run_cmd(cmd, username='root', raise_if_error=False)
    else:
        output = remote_session.run_cmd(cmd, raise_if_error=False)
    
    logger.info(output)
    return output.output
        
def _sudo(cmd, *args, **kwargs):
    return _run(cmd, sudo = True)


@gen.coroutine
def retry(function, *args, **kwargs):
    """ Retries a function up to max_retries, waiting `timeout` seconds between tries.
        This function is designed to retry both boto3 and fabric calls.  In the
        case of boto3, it is necessary because sometimes aws calls return too
        early and a resource needed by the next call is not yet available. """
    logger.info("Entering retry with function %s with args %s and kwargs %s" % (function.__name__, args, kwargs))
    max_retries = kwargs.pop("max_retries", 10)
    timeout = kwargs.pop("timeout", 1)            
    for attempt in range(max_retries):
        try:
            ret = yield thread_pool.submit(function, max_retries=max_retries, *args, **kwargs)
            return ret
        except (ClientError, WaiterError, NetworkError, RemoteCmdExecutionError, 
                EOFError, SSHException, ChannelException, ConnectionError) as e:
            #EOFError can occur in fabric
            logger.error("Failure in %s with args %s and kwargs %s" % (function.__name__, args, kwargs))
            logger.info("retrying %s, (~%s seconds elapsed)" % (function.__name__, attempt * 3))
            yield gen.sleep(timeout)
    else:
        logger.error("Failure in %s with args %s and kwargs %s" % (function.__name__, args, kwargs))
        yield gen.sleep(0.1) #this line exists to allow the logger time to print
        return ("RETRY_FAILED")


@gen.coroutine
def run(cmd, *args, **kwargs):
   ret = yield retry(_run, cmd, *args, **kwargs)
   return ret

@gen.coroutine
def sudo(cmd, *args, **kwargs):
    ret = yield retry(_sudo, cmd ,*args, **kwargs)
    return ret
