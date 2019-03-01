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



from models import Server, Role
from aws_ressources import AWS_INSTANCE_TYPES



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


if os.environ.get('AWS_SPAWNER_TEST'):
    with open("server_config.json", "r") as f:
        SERVER_PARAMS = json.load(f) # load local server parameters
else:
    with open("/etc/jupyterhub/server_config.json", "r") as f:
        SERVER_PARAMS = json.load(f) # load local server parameters

LONG_RETRY_COUNT = 120
HUB_MANAGER_IP_ADDRESS = get_local_ip_address()
NOTEBOOK_SERVER_PORT = 4444
WORKER_USERNAME  = SERVER_PARAMS["WORKER_USERNAME"]
WORKER_IP = None


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
                #start_worker_server will handle starting notebook
                logger.info("start ip and port: %s , %s" % (instance.private_ip_address, NOTEBOOK_SERVER_PORT))
                await self.start_worker_server(instance, new_server=False)
                self.ip = self.user.server.ip = instance.private_ip_address
                self.port = self.user.server.port = NOTEBOOK_SERVER_PORT
            elif instance.state["Name"] in ["stopped", "stopping", "pending", "shutting-down"]:
                #For case that instance is stopped, the attributes are modified
                if instance.state["Name"] == "stopped":
                    self.log.debug('Selected instance type %s for user %s' % (str(self.user_options['INSTANCE_TYPE']), self.user.name))
                    if self.user_options['INSTANCE_TYPE'] in AWS_INSTANCE_TYPES:
                        try:
                            await retry(instance.modify_attribute, Attribute='instanceType', Value = self.user_options['INSTANCE_TYPE'])
                        except:
                            self.log.debug("Instance type for user %s could not be changed." % self.user.name)
                    else:
                        self.log.debug("Instance type for user %s could not recognized." % self.user.name)
                    
                #Server needs to be booted, do so.
                self.log.info("Starting user %s instance " % self.user.name)
                await retry(instance.start, max_retries=LONG_RETRY_COUNT)
                #await retry(instance.start)
                # blocking calls should be wrapped in a Future
                await retry(instance.wait_until_running) #this call can occasionally fail, so we wrap it in a retry.
                self.aws_instance = os.environ['AWS_SPAWNER_WORKER_IP'] = instance.private_ip_address
                await self.start_worker_server(instance, new_server=False)
                self.log.debug("%s , %s" % (instance.private_ip_address, NOTEBOOK_SERVER_PORT))
                # a longer sleep duration reduces the chance of a 503 or infinite redirect error (which a user can
                # resolve with a page refresh). 10s seems to be a good inflection point of behavior
                await asyncio.sleep(10)
                self.ip = self.user.server.ip = instance.private_ip_address
                self.port = self.user.server.port = NOTEBOOK_SERVER_PORT
            elif instance.state["Name"] == "terminated":
                # If the server is terminated ServerNotFound is raised. This leads to the try
                self.log.debug('Instance terminated for user %s. Creating new one and try to attach old volume.' % self.user.name)
                raise ServerNotFound
            else:
                # if instance is in pending, shutting-down, or rebooting state
                raise web.HTTPError(503, "Unknown server state for %s. Please try again in a few minutes" % self.user.name)
        except (ServerNotFound, Server.DoesNotExist) as e:
            self.log.debug('Server not found raised for %s' % self.user.name)

            try:
                self.user.volume = await self.get_volume() #cannot be a thread pool...
            except (VolumeNotFound, Server.DoesNotExist) as e:
                self.user.volume = None
                self.log.debug('Volume not found raised for %s' % self.user.name)

                        
            self.log.info("\nCreate new server for user %s with volume %s\n" % (self.user.name, self.user.volume))
            instance = self.instance =  await self.create_new_instance()
            os.environ['AWS_SPAWNER_WORKER_IP'] = instance.private_ip_address
            await self.start_worker_server(instance, new_server=True)
            # self.notebook_should_be_running = False
            self.log.debug("%s , %s" % (instance.private_ip_address, NOTEBOOK_SERVER_PORT))
            # to reduce chance of 503 or infinite redirect
            await asyncio.sleep(10)
            self.ip = self.user.server.ip
            self.port = self.user.server.port = NOTEBOOK_SERVER_PORT
            
        
        ec2 = boto3.client("ec2", region_name=SERVER_PARAMS["REGION"])
        #update 
        # assign iam role to server
        role = Role.get_role(self.user.name)
        self.log.debug('Trying to attach role %s to instance %s for user %s'
                       % (role.role_arn, self.instance.instance_id ,self.user.name))
        response = await retry(ec2.associate_iam_instance_profile, 
                    IamInstanceProfile={'Arn': role.role_arn , 'Name': role.role_name}, 
                    InstanceId=self.instance.instance_id,
                    max_retries=10)
        self.log.debug('AWS response for tried rolle attachment for user %s: %s' 
                       % (self.user.name, response))
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
            
    async def get_volume(self):
        """ This returns a boto volume resource for the case no volumewas found. 
        If boto can't find the volume or if no entry for instance in database,
            it raises VolumeNotFound error and removes database entry if appropriate """
        self.log.debug("function get_resource for user %s" % self.user.name)
        server = Server.get_server(self.user.name)
        resource = await retry(boto3.resource, "ec2", region_name=SERVER_PARAMS["REGION"])
        try:
            ret = await retry(resource.Volume, server.ebs_volume_id)
            self.log.debug("return for get_volume for user %s: %s" % (self.user.name, ret))
            # boto3.Volume is lazily loaded. Force with .load()
            await retry(ret.load)
            if ret.meta.data is None:
                Server.remove_server(server.server_id)
                self.log.info("\nVolume DNE for user %s\n" % self.user.name)
                raise VolumeNotFound
            return ret
        except ClientError as e:
            self.log.error("get_instance client error: %s" % e)
            if "InvalidInstanceID.NotFound" not in str(e):
                self.log.error("Couldn't find volume for user '%s'" % self.user.name)
                Server.remove_server(server.server_id)
                raise VolumeNotFound
            raise e
            
        
    
    async def start_worker_server(self, instance, new_server=False):
        """ Runs remote commands on worker server to mount user EBS and connect to Jupyterhub. If new_server=True,
            also create filesystem on newly created user EBS"""
        logger.info("function start_worker_server for user %s" % self.user.name)
        # redundant variable set for get_args()
        self.ip = self.user.server.ip = instance.private_ip_address
        self.port = self.user.server.port = NOTEBOOK_SERVER_PORT
        # self.user.server.port = NOTEBOOK_SERVER_PORT
        try:
            # Wait for server to finish booting...
            wait_result = await self.wait_until_SSHable(instance.private_ip_address,max_retries=LONG_RETRY_COUNT)
            # If first time server then setup the user name
            if new_server:
                await self.setup_user(instance.private_ip_address)
            #start notebook
            self.log.error("\n\n\n\nabout to check if notebook is running before launching\n\n\n\n")
            notebook_running = await self.is_notebook_running(instance.private_ip_address)
            if not notebook_running:
                await self.remote_notebook_start(instance)
        except RemoteCmdExecutionError:
            # terminate instance and create a new one
            raise web.HTTPError(500, "Instance unreachable")

    async def setup_user(self, privat_ip):
        """ setup_user_home  and prepare s3"""

        if self.user.name == WORKER_USERNAME:
            pass
        else:
            if SERVER_PARAMS["USER_HOME_EBS_SIZE"] > 0:
                with settings(**FABRIC_DEFAULTS, host_string=privat_ip):
                    await sudo("mkfs.xfs /dev/%s" %("xvdf") , user="root",  pty=False)
                    await sudo("mkdir /jupyteruser", user="root",  pty=False)
                    await sudo("echo /dev/%s /jupyteruser xfs defaults 1 1 >> /etc/fstab" %("xvdf") , user="root",  pty=False)
                    await sudo("mount -a" , user="root",  pty=False)
            with settings(**FABRIC_DEFAULTS, host_string=privat_ip):
                await sudo("mkdir -p /jupyteruser" , user="root",  pty=False)
                await sudo("useradd -d /home/%s %s -s /bin/bash  &>/dev/null" % (self.user.name,self.user.name) , user="root",  pty=False)
                await sudo("cp -R /home/%s /jupyteruser/%s" % (WORKER_USERNAME,self.user.name), user="root",  pty=False)
                await sudo("ln -s /jupyteruser/%s /home/%s" % (self.user.name,self.user.name), user="root",  pty=False)
                await sudo("chown -R %s /home/%s /jupyteruser/%s" %(self.user.name,self.user.name,self.user.name), user="root",  pty=False)
#                await sudo("chown -R %s.%s /home/%s /jupyteruser/%s" %(self.user.name,self.user.name,self.user.name,self.user.name), user="root",  pty=False)
                await sudo("echo \" %s ALL=(ALL) NOPASSWD:ALL \" > /etc/sudoers.d/%s " % (self.user.name,self.user.name), user="root",  pty=False)
                # uncomment the line below to setup a default password for the user.
                #await sudo('echo -e "%s\n%s" | passwd %s' % (self.user.name,self.user.name,self.user.name), pty=False)

        return True
#"echo jupyterhub-bucket:/freelancer/%s /jupyteruser/s3 fuse.s3fs _netdev,iam_role=auto,use_cache=/tmp 0 0 >> /etc/fstab"
    def user_env(self, env): 
        """Augment environment of spawned process with user specific env variables.""" 
        import pwd 
        # set HOME and SHELL for the Jupyter process 
        env['HOME'] = '/home/' + self.user.name
        env['SHELL'] = '/bin/bash'
        return env 

 
    def get_env(self):
        """Get the complete set of environment variables to be set in the spawned process."""
        env = super().get_env()
        env = self.user_env(env)
        return env

    
    async def remote_notebook_start(self, instance):
        """ Do notebook start command on the remote server."""
        # Setup environments
        env = self.get_env()
        lenv=''
        for key in env:
            lenv = lenv + key + "=" + env[key] + " "
        # End setup environment
        self.log.debug("function remote_server_start %s" % self.user.name)
        worker_ip_address_string = instance.private_ip_address
        start_notebook_cmd = self.cmd + self.get_args()
        start_notebook_cmd = " ".join(start_notebook_cmd)
        self.log.info("Starting user %s jupyterhub" % self.user.name)
        with settings(user = self.user.name, key_filename = FABRIC_DEFAULTS["key_filename"],  host_string=worker_ip_address_string):
             await sudo("%s %s --user=%s --notebook-dir=/ --allow-root > /tmp/jupyter.log 2>&1 &" % (lenv, start_notebook_cmd,self.user.name),  pty=False)
            
        self.log.debug("Just started the notebook for user %s with following command, waiting." % self.user.name)
        self.log.debug("%s %s --user=%s --notebook-dir=/ --allow-root > /tmp/jupyter.log 2>&1 &" % (lenv, start_notebook_cmd,self.user.name))
        try:
            self.user.settings[self.user.name] = instance.public_ip_address
        except:
            self.user.settings[self.user.name] = ""
        # self.notebook_should_be_running = True
        await self.is_notebook_running(worker_ip_address_string, attempts=30)
        
    async def create_new_instance(self):
        """ Creates and boots a new server to host the worker instance."""
        self.log.debug("function create_new_instance %s" % self.user.name)
        ec2 = boto3.client("ec2", region_name=SERVER_PARAMS["REGION"])
        resource = boto3.resource("ec2", region_name=SERVER_PARAMS["REGION"])
        BDM = []
        boot_drive = {'DeviceName': '/dev/sda1',  # this is to be the boot drive
                      'Ebs': {'VolumeSize': SERVER_PARAMS["WORKER_EBS_SIZE"],  # size in gigabytes
                              'DeleteOnTermination': True,
                              'VolumeType': 'gp2',  # This means General Purpose SSD
                              # 'Iops': 1000 }  # i/o speed for storage, default is 100, more is faster
                              }
                     }
        BDM = [boot_drive]  
        # get user volume
        volume_id = await self.select_volume()
        # create new instance
        reservation = await retry(
                ec2.run_instances,
                ImageId=SERVER_PARAMS["WORKER_AMI"],
                MinCount=1,
                MaxCount=1,
                KeyName=SERVER_PARAMS["KEY_NAME"],
                InstanceType=self.user_options['INSTANCE_TYPE'],
                SubnetId=SERVER_PARAMS["SUBNET_ID"],
                SecurityGroupIds=SERVER_PARAMS["WORKER_SECURITY_GROUPS"],
                BlockDeviceMappings=BDM,
        )
        self.log.debug(reservation)
        try:
            instance_id = reservation["Instances"][0]["InstanceId"]
        except TypeError as e:
            raise Exception('AWS sends weirdly formatted JSON. Please try again...')
            
        instance = await retry(resource.Instance, instance_id)
        #if an old volume is restored from a terminated instance, the user has to be updated, e.g. delted and newly saved
        # TODO: Support multiple volumes
        if self.user.volume:
            server = Server.get_server(self.user.name)
            Server.remove_server(server.server_id)
        Server.new_server(instance_id, self.user.name, volume_id)
        await retry(instance.wait_until_exists)
        # add server tags; tags cannot be added until server exists
        await retry(instance.create_tags, Tags=WORKER_TAGS)
        await retry(instance.create_tags, Tags=[{"Key": "User", "Value": self.user.name}])
        await retry(instance.create_tags, Tags=[{"Key": "Name", "Value": SERVER_PARAMS["WORKER_SERVER_NAME"] + '_' + self.user.name}])

        # start server
        # blocking calls should be wrapped in a Future
        await retry(instance.wait_until_running)
        # Attach persistent EBS
        await retry(instance.attach_volume, 
                    Device='/dev/sdf', 
                    VolumeId = volume_id, 
                    InstanceId = instance_id)
        return instance
    
    async def select_volume(self):
        '''Handles volume selection'''
        ec2_vol = boto3.client("ec2", region_name=SERVER_PARAMS["REGION"])
        resource_vol = boto3.resource("ec2", region_name=SERVER_PARAMS["REGION"])
         # Default: set if user had an old volume in db
        if self.user.volume:
            volume_id = self.user.volume.id
        # if no volume found in db we create a new one, attach an old one or create new from snapshot
        elif self.user_options['EBS_VOL_ID']:
            volume_id = self.user_options['EBS_VOL_ID']
        elif self.user_options['EBS_VOL_SIZE'] > 0:
            volume = await retry(ec2_vol.create_volume, AvailabilityZone = SERVER_PARAMS["REGION"]+'b', 
                                       Size = self.user_options['EBS_VOL_SIZE'], 
                                       VolumeType = 'gp2')
            volume_id = volume['VolumeId']
            await retry(resource_vol.create_tags, Resources=[volume_id], Tags=[{"Key": "Name", "Value": 'jhub_worker_volume_' + self.user.name}])
        elif self.user_options['EBS_SNAP_ID']:
            volume = await retry(ec2_vol.create_volume, AvailabilityZone = SERVER_PARAMS["REGION"]+'b', 
                                       Size = self.user_options['EBS_VOL_SIZE'], 
                                       SnapshotId=self.user_options['EBS_SNAP_ID'],
                                       VolumeType = 'gp2')
            volume_id = volume['VolumeId']
            await retry(resource_vol.create_tags, Resources=[volume_id], Tags=[{"Key": "Name", "Value": 'jhub_worker_volume_' + self.user.name}])
        else:
            raise Exception('No EBS volume-id or volume size provided.')
        
        return volume_id

        
    
    
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
        return open('options_form.html','r').read()

    
