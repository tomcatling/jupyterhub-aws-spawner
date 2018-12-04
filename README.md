## Deprecated due to use of kubespawner and Jupyterhub on Kubernetes

# jupyterhub-aws-spawner
Custom spawner for launching and organizing EC2 instances with jupyterhub, using boto3 and tornado.


### So far:

- Scaling instance size via options form

- Keeping track of EBS volume even if instance is terminated

- Attaching and mounting custom EBS volumes

- Create and attach new volume from snapshot

- Assignment of IAM roles (via DB entry)


### Todo:

- Hardening and testing (Data integrity first)

- Automatically mounting S3 buckets via s3fs and IAM

- Make options form more shiny and userfriendly



### Long term todo:

- S3 bucket lookup and selection in options

- Handling multiple volumes per user

- Share volumes with other users

- Multiple instances per User

- Decouple from AWS -> jupyterhub-cloud-spawner




![alt text](https://raw.githubusercontent.com/idalab-de/jupyterhub-aws-spawner/master/options_screen.png)



## General setup
Its strongly recommended to set up a AWS VPC configuration according to [cloudJhub](https://github.com/harvard/cloudJHub) and configure your `server_config.json` as shown below. This repo is built upon the spawner from cloudJhub and the instructions there are good guidance to create a running environemnt.

```json
  {"JUPYTER_MANAGER_IP": "", 
  "MANAGER_IP_ADDRESS": "", 
  "SERVER_USERNAME": "", 
  "JUPYTER_CLUSTER": "", 
  "WORKER_USERNAME": "", 
  "REGION": "", 
  "SUBNET_ID": "", 
  "WORKER_EBS_SIZE": , 
  "WORKER_SERVER_OWNER": "", 
  "AVAILABILITY_ZONE": " {a,b,c}", 
  "WORKER_SERVER_NAME": "", 
  "USER_HOME_EBS_SIZE": , 
  "KEY_NAME": "jupyter_key.pem", 
  "WORKER_AMI": "", 
  "WORKER_SECURITY_GROUPS": [""], 
  "JUPYTER_NOTEBOOK_TIMEOUT": 3600, 
  "INSTANCE_TYPE": ""
  }
```

Next create a `bastion_info.json` to ssh-jump to the server in the subnet. Use the key from the configuration above.

Note: To make jumpssh work you should set `StrictHostKeyChecking no` on the bastion server for the worker subnet, otherwise the jumper will be stuck.
```json
{
  "bastion" : "example.bastion.com",
  "key_path" : "/path/to/.ssh/jupyter_key.pem",
  "user": "hostuser"
}
```

If you want to use an external db you can create a `db_creds.json`. Every MySqlAlchemy supported DB should work but Postgres is recommended by Jupyterhub and this way you can use one DB for Jupyterhub and spawner meta information. Otherwise sqlite can be used.
```json
{
  "database" : "dbname", 
  "user" : "dbuser", 
  "password":"dbpw",
  "host":"dburl", 
  "port":""
}```

