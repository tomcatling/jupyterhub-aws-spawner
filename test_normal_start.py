#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Used for testing the start of an instance
"""

import os
from types import SimpleNamespace
# Set env to make debugging in interactive shell more comfortable
os.environ['AWS_SPAWNER_TEST'] = '1'
import spawner
from models import Server
import asyncio

#%% Configure
class DummyUser():

    def __init__(self, name,):
        self.name = name
        self.last_activity = ''
        self.server = SimpleNamespace(**{'ip' : '',
                                         'base_url': ''})
        self.url = ''
        self.settings = {}
    
dummyUser = DummyUser(name='developmentUser')   
     
try:
    server = Server.get_server(user_id=dummyUser.name)
except:
    server = Server.new_server(server_id = '' , user_id = dummyUser.name , ebs_volume_id = '')
assert server.user_id == dummyUser.name
    

dummyUserOptions = {'EBS_VOL_ID' : '',
                    'EBS_VOL_SIZE' : 3,
                    'INSTANCE_TYPE': 't2.nano'}

dummyHubOptions = SimpleNamespace(**{'public_host' : '',
                                     'api_url' : '',
                                     'base_url' : ''})

dummyApiToken = open('api_token.txt','r').read()

dummyOAuthID = '1234'

#%% 

instanceSpawner = spawner.InstanceSpawner()
instanceSpawner.set_debug_options(dummyUser = dummyUser, dummyUserOptions=dummyUserOptions, 
                                  dummyHubOptions=dummyHubOptions, dummyApiToken = dummyApiToken,
                                  dummyOAuthID = dummyOAuthID)

#%%


async def terminate(instance):
    ret = await instance.terminate(delete_volume=True)
    return ret

async def start(instance):
    ret_start = await instance.start()
    ret_term = await terminate(instanceSpawner)
    return ret_start, ret_term

loop = asyncio.get_event_loop()
start_output = loop.run_until_complete(start(instanceSpawner))





