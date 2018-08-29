# jupyterhub-aws-spawner
Custom spawner for launching and organizing EC2 instances with jupyterhub.


So far:

-Scaling instance size via options form

-Keeping track of EBS volume even if instance is terminated

-Attaching and mounting custom EBS volumes

-Create and attach new volume from snapshot

-Assignment of IAM roles (via DB entry)


Todo:

-Hardening and testing (Data integrity first)

-Automatically mounting S3 buckets via s3fs and IAM



Long term todo:

-Handling multiple volumes per user

-Bucket lookup and selection in options

-Attach Elastic GPU






![alt text](https://raw.githubusercontent.com/idalab-de/jupyterhub-aws-spawner/master/options_screen.png)

