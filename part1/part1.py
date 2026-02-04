#!/usr/bin/env python3

import time
import googleapiclient.discovery
import google.auth

credentials, project = google.auth.default()
service = googleapiclient.discovery.build('compute', 'v1', credentials=credentials)

ZONE = 'us-west1-b'
INSTANCE_NAME = 'flask-vm'
MACHINE_TYPE = 'e2-standard-2'   #  f1-micro

STARTUP_SCRIPT = """#!/bin/bash
set -e
mkdir -p /opt/app
cd /opt/app
apt-get update
apt-get install -y python3 python3-pip git
git clone https://github.com/cu-csci-4253-datacenter/flask-tutorial
cd flask-tutorial
python3 setup.py install
pip3 install -e .
export FLASK_APP=flaskr
flask init-db
nohup flask run -h 0.0.0.0 -p 5000 &
"""

#
# Stub code - just lists all instances
#
def list_instances(compute, project, zone):
    result = compute.instances().list(project=project, zone=zone).execute()
    return result['items'] if 'items' in result else []

def wait_for_operation(op, zone=None):
    while True:
        if zone:
            result = service.zoneOperations().get(
                project=project, zone=zone, operation=op['name']).execute()
        else:
            result = service.globalOperations().get(
                project=project, operation=op['name']).execute()

        if result['status'] == 'DONE':
            if 'error' in result:
                print(result['error'])
                raise SystemExit("Operation failed")
            return
        time.sleep(2)

def create_firewall_rule():
    firewall_body = {
        'name': 'allow-5000',
        'network': 'global/networks/default',
        'direction': 'INGRESS',
        'sourceRanges': ['0.0.0.0/0'],
        'targetTags': ['allow-5000'],
        'allowed': [{'IPProtocol': 'tcp', 'ports': ['5000']}],
    }
    try:
        op = service.firewalls().insert(project=project, body=firewall_body).execute()
        wait_for_operation(op)
    except Exception as e:
        print("Firewall allow-5000 already exists, skip.")

def create_instance():
    image = service.images().getFromFamily(
        project='ubuntu-os-cloud',
        family='ubuntu-2204-lts').execute()

    config = {
        'name': INSTANCE_NAME,
        'machineType': f'zones/{ZONE}/machineTypes/{MACHINE_TYPE}',
        'tags': {'items': ['allow-5000']},
        'disks': [{
            'boot': True,
            'autoDelete': True,
            'initializeParams': {
                'sourceImage': image['selfLink'],
                'diskSizeGb': '10',
            }
        }],
        'networkInterfaces': [{
            'network': 'global/networks/default',
            'accessConfigs': [{
                'name': 'External NAT',
                'type': 'ONE_TO_ONE_NAT'
            }]
        }],
        'metadata': {
            'items': [{
                'key': 'startup-script',
                'value': STARTUP_SCRIPT
            }]
        }
    }

    op = service.instances().insert(
        project=project, zone=ZONE, body=config).execute()
    wait_for_operation(op, ZONE)

def get_external_ip():
    inst = service.instances().get(
        project=project, zone=ZONE, instance=INSTANCE_NAME).execute()
    for nic in inst['networkInterfaces']:
        for ac in nic.get('accessConfigs', []):
            if 'natIP' in ac:
                return ac['natIP']
    return None

print("Your running instances are:")
for instance in list_instances(service, project, ZONE):
    print(instance['name'])

create_firewall_rule()
create_instance()

ip = get_external_ip()
print("\nVisit:")
print(f"http://{ip}:5000")
