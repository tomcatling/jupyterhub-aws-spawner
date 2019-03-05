'''
Intially created by  Faras Sadek from https://github.com/harvard/cloudJHub.
Now further developed by Hagen Hoferichter from idalab.de
'''

import json
import logging
import socket
import boto3
import os
from fabric.api import env, sudo as _sudo, run as _run
from fabric.context_managers import settings
from fabric.exceptions import NetworkError
from paramiko.ssh_exception import SSHException, ChannelException
from botocore.exceptions import ClientError, WaiterError
from datetime import datetime
from tornado import web
from jupyterhub.spawner import Spawner
import asyncio
#from concurrent.futures import ThreadPoolExecutor


from jupyterhub_aws_spawner.models import Server, Role
from jupyterhub_aws_spawner.aws_ressources import AWS_INSTANCE_TYPES


def get_local_ip_address():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    ip_address = s.getsockname()[0]
    s.close()
    return ip_address

class ResourceNotFound(Exception):
    pass

class ServerNotFound(ResourceNotFound):
    print('Server not found in database')
    pass

class VolumeNotFound(ResourceNotFound):
    print('Volume not found in database')
    pass


SERVER_TEMPLATE_URL = os.environ['ServerTemplateUrl']
SERVER_KEY_NAME = os.environ['ServerKeyName']
PARENT_STACK = os.environ['ParentStack']

LONG_RETRY_COUNT = 120
HUB_MANAGER_IP_ADDRESS = get_local_ip_address()
NOTEBOOK_SERVER_PORT = 80
WORKER_USERNAME  = "jovyan"
WORKER_IP = None

SERVER_PARAMS =   {
  "JUPYTER_MANAGER_IP": "", 
  "MANAGER_IP_ADDRESS": "", 
  "SERVER_USERNAME": "admin", 
  "JUPYTER_CLUSTER": "", 
  "WORKER_USERNAME": "", 
  "REGION": "eu-west-2", 
  "SUBNET_ID": "", 
  "WORKER_EBS_SIZE": "", 
  "WORKER_SERVER_OWNER": "", 
  "AVAILABILITY_ZONE": "a", 
  "WORKER_SERVER_NAME": "", 
  "USER_HOME_EBS_SIZE": "", 
  "KEY_NAME": SERVER_KEY_NAME, 
  "WORKER_AMI": "", 
  "WORKER_SECURITY_GROUPS": [""], 
  "JUPYTER_NOTEBOOK_TIMEOUT": 3600, 
  "INSTANCE_TYPE": ""
}

WORKER_TAGS = [ #These tags are set on every server created by the spawner
    {"Key": "Owner", "Value": SERVER_PARAMS["WORKER_SERVER_OWNER"]},
    {"Key": "Creator", "Value": SERVER_PARAMS["WORKER_SERVER_OWNER"]},
    {"Key": "Jupyter Cluster", "Value": SERVER_PARAMS["JUPYTER_CLUSTER"]},
]

#thread_pool = ThreadPoolExecutor(100)

#Logging settings
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


#Global Fabric config
class RemoteCmdExecutionError(Exception): pass
env.abort_exception = RemoteCmdExecutionError
env.abort_on_prompts = True

FABRIC_DEFAULTS = {"user":SERVER_PARAMS["WORKER_USERNAME"],
                   "key_filename":"/home/%s/.ssh/%s" % (SERVER_PARAMS["SERVER_USERNAME"], SERVER_PARAMS["KEY_NAME"])}

FABRIC_QUIET = True
#FABRIC_QUIET = False
# Make Fabric only print output of commands when logging level is greater than warning.

if os.environ.get('AWS_SPAWNER_TEST'):
    from ssh_run_debug import _run, _sudo
    async def run(cmd, *args, **kwargs):
       ret = await retry(_run, cmd , sudo = False, *args, **kwargs)
       return ret
    
    async def sudo(cmd, *args, **kwargs):
        ret = await retry(_sudo, cmd, *args, **kwargs)
        return ret
else:
    from fabric.api import sudo as _sudo, run as _run
    async def sudo(*args, **kwargs):
        ret = await retry(_sudo, *args, **kwargs, quiet=FABRIC_QUIET)
        return ret
    
    async def run(*args, **kwargs):
        ret = await retry(_run, *args, **kwargs, quiet=FABRIC_QUIET)
        return ret


    
async def retry(function, *args, **kwargs):
    """ Retries a function up to max_retries, waiting `timeout` seconds between tries.
        This function is designed to retry both boto3 and fabric calls.  In the
        case of boto3, it is necessary because sometimes aws calls return too
        early and a resource needed by the next call is not yet available. """
    logger.info("Entering retry with function %s with args %s and kwargs %s" % (function.__name__, args, kwargs))
    max_retries = kwargs.pop("max_retries", 10)
    timeout = kwargs.pop("timeout", 1)            
    for attempt in range(max_retries):
        try:
#            ret = thread_pool.submit(function, *args, **kwargs)
            ret = function(*args, **kwargs)
            return ret
        except (ClientError, WaiterError, NetworkError, RemoteCmdExecutionError, EOFError, SSHException, ChannelException) as e:
            #EOFError can occur in fabric
            logger.error("Failure in %s with args %s and kwargs %s" % (function.__name__, args, kwargs))
            logger.info("retrying %s, (~%s seconds elapsed)" % (function.__name__, attempt * 3))
            asyncio.sleep(timeout)
    else:
        logger.error("Failure in %s with args %s and kwargs %s" % (function.__name__, args, kwargs))
        asyncio.sleep(0.1) #this line exists to allow the logger time to print
        return ("RETRY_FAILED")

#########################################################################################################
#########################################################################################################

class InstanceSpawner(Spawner):
    """ A Spawner that starts an EC2 instance for each user.

        Warnings:
            - Because of db.commit() calls within Jupyterhub's code between await calls in jupyterhub.user.spawn(),
            setting an attribute on self.user.server results in ORM calls and incomplete jupyterhub.sqlite Server
            entries. Be careful of setting self.user.server attributes too early in this spawner.start().

            In this spawner's start(), self.user.server.ip and self.user.server.port are set immediately before the
            return statement to alleviate the edge case where they are not always set in Jupyterhub v0.6.1. An
            improvement is made in developmental version Jupyterhub v0.7.0 where they are explicitly set.

            - It's possible for the logger to be terminated before log is printed. If your stack traces do not match up
            with your log statements, insert a brief sleep into the code where your are logging to allow time for log to
            flush.
        """
    def set_debug_options(self, dummyUser = None, dummyUserOptions = None, 
                          dummyHubOptions= None, dummyServerOptions = None,
                          dummyApiToken = None, dummyOAuthID = None):
        if dummyUser:
            self.user = dummyUser   
        if dummyUserOptions:
            self.user_options = dummyUserOptions   
        if dummyHubOptions:
            self.hub = dummyHubOptions   
        if dummyServerOptions:
            self.server = dummyServerOptions
        if dummyApiToken:
            self.api_token = dummyApiToken
        if dummyOAuthID:
            self.oauth_client_id = dummyOAuthID
        
            
    async def start(self):
        
        """ When user logs in, start their instance.
            Must return a tuple of the ip and port for the server and Jupyterhub instance. """
            
            
        self.log.debug("function start for user %s" % self.user.name)
        self.user.last_activity = datetime.utcnow()
        try:
            instance = self.instance = await self.get_instance() #cannot be a thread pool...
            os.environ['AWS_SPAWNER_WORKER_IP'] = instance.private_ip_address if type(instance.private_ip_address) == str else "NO IP"
            #comprehensive list of states: pending, running, shutting-down, terminated, stopping, stopped.
            if instance.state["Name"] == "running":
                ec2_run_status = await self.check_for_hanged_ec2(instance)
                if ec2_run_status == "SSH_CONNECTION_FAILED":
                    #await self.poll()
                    #await self.kill_instance(instance)
                    #await retry(instance.start, max_retries=(LONG_RETRY_COUNT*2))
                    #await retry(instance.wait_until_running, max_retries=(LONG_RETRY_COUNT*2)) #this call can occasionally fail, so we wrap it in a retry.
                    #return instance.private_ip_address, NOTEBOOK_SERVER_PORT
                    return None
                logger.info("start ip and port: %s , %s" % (instance.private_ip_address, NOTEBOOK_SERVER_PORT))
                self.ip = self.user.server.ip = instance.private_ip_address
                self.port = self.user.server.port = NOTEBOOK_SERVER_PORT
            elif instance.state["Name"] in ["stopped", "stopping", "pending", "shutting-down"]:
                # we should tear down the cfn stack here
                pass
            elif instance.state["Name"] == "terminated":
                # If the server is terminated ServerNotFound is raised. This leads to the try
                self.log.debug('Instance terminated for user %s. Creating new one.' % self.user.name)
                raise ServerNotFound
            else:
                # if instance is in pending, shutting-down, or rebooting state
                raise web.HTTPError(503, "Unknown server state for %s. Please try again in a few minutes" % self.user.name)
        except (ServerNotFound, Server.DoesNotExist) as e:
            self.log.debug('Server not found raised for %s' % self.user.name)

                        
            self.log.info("\nCreate new server for user %s \n" % (self.user.name))

            self.create_stack()
            instance = self.instance =  await self.get_new_instance


            os.environ['AWS_SPAWNER_WORKER_IP'] = instance.private_ip_address
            # self.notebook_should_be_running = False
            self.log.debug("%s , %s" % (instance.private_ip_address, NOTEBOOK_SERVER_PORT))
            # to reduce chance of 503 or infinite redirect
            await asyncio.sleep(10)
            self.ip = self.user.server.ip
            self.port = self.user.server.port = NOTEBOOK_SERVER_PORT
            
        return instance.private_ip_address, NOTEBOOK_SERVER_PORT
        
        
    def clear_state(self):
        """Clear stored state about this spawner """
        super(InstanceSpawner, self).clear_state()

    async def stop(self, now=False):
        """ When user session stops, stop user instance """
        self.log.debug("function stop")
        self.log.info("Stopping user %s instance " % self.user.name)
        try:
            instance = await self.get_instance()  
            await retry(instance.stop)
            return 'Notebook stopped'
            # self.notebook_should_be_running = False
        except Server.DoesNotExist:
            self.log.error("Couldn't stop server for user '%s' as it does not exist" % self.user.name)
            # self.notebook_should_be_running = False
        self.clear_state()

    async def terminate(self, now=False, delete_volume=False):
        """ Terminate instance for debugging purposes """
        self.log.debug("function terminate")
        self.log.info("Terminating user %s instance " % self.user.name)
        try:
            instance = await self.get_instance()  
            await retry(instance.terminate)
            if delete_volume:
                server = Server.get_server(self.user.name)  
                ec2 = boto3.client("ec2", region_name=SERVER_PARAMS["REGION"])
                await retry(ec2.delete_volume, VolumeId = server.ebs_volume_id, max_retries=40)
            return 'Terminated'
            # self.notebook_should_be_running = False
        except Server.DoesNotExist:
            self.log.error("Couldn't terminate server for user '%s' as it does not exist" % self.user.name)
            # self.notebook_should_be_running = False
        self.clear_state()
        
    async def kill_instance(self,instance):
        self.log.debug(" Kill hanged user %s instance:  %s " % (self.user.name,instance.id))
        await self.stop(now=True)
        

    # Check if the machine is hanged
    async def check_for_hanged_ec2(self, instance):
        timerightnow    = datetime.utcnow().replace(tzinfo=None)
        ec2launchtime   = instance.launch_time.replace(tzinfo=None)
        ec2uptimeSecond = (timerightnow - ec2launchtime).seconds
        #conn_health = None
        conn_health = ""
        if ec2uptimeSecond > 180:
            # wait_until_SSHable return : 1) "some object" if SSH is established;  2) "SSH_CONNECTION_FAILED" otherwise
            conn_health  = await self.wait_until_SSHable(instance.private_ip_address,max_retries=5)
        return(conn_health)


    async def poll(self):
        """ Polls for whether process is running. If running, return None. If not running,
            return exit code """
        self.log.debug("function poll for user %s" % self.user.name)
        try:
            instance = await self.get_instance()
            self.log.debug(instance.state)
            if instance.state['Name'] == 'running':
                self.log.debug("poll: server is running for user %s" % self.user.name)
                # We cannot have this be a long timeout because Jupyterhub uses poll to determine whether a user can log in.
                # If this has a long timeout, logging in without notebook running takes a long time.
                # attempts = 30 if self.notebook_should_be_running else 1
                # check if the machine is hanged 
                ec2_run_status = await self.check_for_hanged_ec2(instance)
                if ec2_run_status == "SSH_CONNECTION_FAILED":
                    #self.log.debug(ec2_run_status)
                    await self.kill_instance(instance)
                    return "Instance Hang"
                else:
                    notebook_running = await self.is_notebook_running(instance.private_ip_address, attempts=1)
                    if notebook_running:
                        self.log.debug("poll: notebook is running for user %s" % self.user.name)
                        return None #its up!
                    else:
                        self.log.debug("Poll, notebook is not running for user %s" % self.user.name)
                        return "server up, no instance running for user %s" % self.user.name
            else:
                self.log.debug("instance waiting for user %s" % self.user.name)
                return "instance stopping, stopped, or pending for user %s" % self.user.name
        except Server.DoesNotExist:
            self.log.error("Couldn't poll server for user '%s' as it does not exist" % self.user.name)
            # self.notebook_should_be_running = False
            return "Instance not found/tracked"
    
    ################################################################################################################
    ### helpers ###

    async def is_notebook_running(self, ip_address_string, attempts=1):
        """ Checks if jupyterhub/notebook is running on the target machine, returns True if Yes, False if not.
            If an attempts count N is provided the check will be run N times or until the notebook is running, whichever
            comes first. """
        with settings(**FABRIC_DEFAULTS, host_string=ip_address_string):
            for i in range(attempts):
                self.log.info("function check_notebook_running for user %s, attempt %s..." % (self.user.name, i+1))
                output = await run("ps -ef | grep jupyterhub-singleuser")
                for line in output.splitlines(): #
                    #if "jupyterhub-singleuser" and NOTEBOOK_SERVER_PORT in line:
                    # TODO: Check for notebook command from jhub config 
                    if "jupyterhub-singleuser" and str(NOTEBOOK_SERVER_PORT)  in str(line):
                        self.log.info("the following notebook is definitely running:")
                        self.log.info(line)
                        return True
                self.log.info("Notebook for user %s not running..." % self.user.name)
                await asyncio.sleep(1)
            self.log.error("Notebook for user %s is not running." % self.user.name)
            return False


    ###  Retun SSH_CONNECTION_FAILED if ssh connection failed
    async def wait_until_SSHable(self, ip_address_string, max_retries=1):
        """ Run a meaningless bash command (a comment) inside a retry statement. """
        self.log.debug("function wait_until_SSHable for user %s" % self.user.name)
        with settings(**FABRIC_DEFAULTS, host_string=ip_address_string):
            ret = await run("# waiting for ssh to be connectable for user %s with ip %s and fabric settings %s..." % (self.user.name, ip_address_string, FABRIC_DEFAULTS), max_retries=max_retries)
        if ret == "RETRY_FAILED":
           ret = "SSH_CONNECTION_FAILED"
        return (ret)



    async def get_instance(self):
        """ This returns a boto Instance resource; if boto can't find the instance or if no entry for instance in database,
            it raises ServerNotFound error and removes database entry if appropriate """
        logger.info("function get_instance for user %s" % self.user.name)
        server = Server.get_server(self.user.name)
        resource = await retry(boto3.resource, "ec2", region_name=SERVER_PARAMS["REGION"])
        try:
            ret = await retry(resource.Instance, server.server_id)
            logger.info("return for get_instance for user %s: %s" % (self.user.name, ret))
            # boto3.Instance is lazily loaded. Force with .load()
            await retry(ret.load)
            if ret.meta.data is None:
                raise ServerNotFound
            return ret
        except ClientError as e:
            self.log.error("get_instance client error: %s" % e)
            if "InvalidInstanceID.NotFound" not in str(e):
                self.log.error("Couldn't find instance for user '%s'" % self.user.name)
                Server.remove_server(server.server_id)
                raise ServerNotFound
            raise e
            
        
    async def create_stack(self):
        """ Creates and boots a new server to host the worker instance."""
        self.log.debug("function create_new_instance %s" % self.user.name)

        stackname = f'{self.user.name}-server'

        client = boto3.client("cloudformation", region_name='eu-west-2')
        client.create_stack(
                StackName=stackname,
                TemplateURL=SERVER_TEMPLATE_URL,
                Parameters=[
                    {"ParameterKey": "User", "ParameterValue": str(self.user.name)},
                    {"ParameterKey": "KeyName", "ParameterValue": str(SERVER_KEY_NAME)},
                    {"ParameterKey": "ParentStack", "ParameterValue": str(PARENT_STACK)},
                ],
        )

    async def get_new_instance(self)
        self.log.debug("function create_new_instance %s" % self.user.name)

        stackname = f'{self.user.name}-server'

        waiter = client.get_waiter('stack_create_complete')
        await retry(waiter.wait,StackName=stackname)

        response = client.describe_stack_resources(StackName=stackname)
        instances = [i for i in response['StackResources'] if i['ResourceType']=='AWS::EC2::Instance']

        instance_id = instances[0]['PhysicalResourceId']

        ec2 = boto3.resource("ec2", region_name=SERVER_PARAMS["REGION"])
        
        instance = await retry(ec2.Instance, instance_id)

        return instance


    def options_from_form(self, formdata):
        '''
        Parses arguments from the options form to pass to the spawner.
        Output can be accessed via self.user_options
        '''
        options = {}
        self.log.debug(str(formdata))
        inst_type = formdata['instance_type'][0].strip()
        ebs_vol_id = formdata['ebs_vol_id'][0].strip()
        ebs_vol_size = formdata['ebs_vol_size'][0].strip()
        ebs_snap_id = formdata['ebs_snap_id'][0].strip()
        
        options['INSTANCE_TYPE'] = inst_type if inst_type else ''
        options['EBS_VOL_ID'] = ebs_vol_id if ebs_vol_id else ''
        options['EBS_SNAP_ID'] = ebs_snap_id if ebs_snap_id else ''
        options['EBS_VOL_SIZE'] = int(ebs_vol_size) if ebs_vol_size else 0
        self.log.debug(str(options))
        return options
    
    def _options_form_default(self):
#         default_env = "YOURNAME=%s\n" % self.user.name
#         return """
#         <label for="args">Extra notebook CLI arguments</label>
#         <input name="args" placeholder="e.g. --debug"></input>
#         <label for="env">Environment variables (one per line)</label>
#         <textarea name="env">{env}</textarea>
#         """.format(env=default_env)
        dirname = os.path.dirname(__file__)
        filename = os.path.join(dirname, 'options_form.html')
        return open(filename,'r').read()

    
