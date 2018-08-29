# jupyterhub-aws-spawner
Custom spawner for launching and organizing EC2 instances with jupyterhub.


So far:

-Scaling instance size via options form

-Keeping track of EBS volume even if instance is termianted

-Attaching and mounting custom EBS volumes

-Create and attach new volume from snapshot

-Attachment of IAM roles (via DB Admin)


Todo:

-Mounting S3 buckets via s3fs and IAM

-Hardening and testing (Data integrity first)


Long term todo:

-Handling multiple volumes 

-Bucket lookup and selection on start

-Attach Elastic GPU






![alt text](https://raw.githubusercontent.com/idalab-de/jupyterhub-aws-spawner/master/options_screen.png)

