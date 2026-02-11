#!/usr/bin/env python3
import argparse
import json
import os
import time

import googleapiclient.discovery
import google.oauth2.service_account as service_account

BASE_ZONE = "us-west1-a"

IMAGE_PROJECT = "ubuntu-os-cloud"
IMAGE_FAMILY = "ubuntu-2204-lts"

MACHINE_TYPE_VM1 = "e2-medium"
MACHINE_TYPE_VM2 = "e2-medium"
SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


# VM-2 startup: same method as Part1 
VM2_STARTUP = r"""#!/bin/bash
set -eux

mkdir -p /opt/app
cd /opt/app

apt-get update -y
apt-get install -y python3 python3-pip git

rm -rf flask-tutorial
git clone https://github.com/cu-csci-4253-datacenter/flask-tutorial
cd flask-tutorial

python3 setup.py install
pip3 install -e .

export FLASK_APP=flaskr
flask init-db

nohup flask run -h 0.0.0.0 -p 5000 >/var/log/flask.log 2>&1 &
"""


# VM-1 launcher (runs on VM1): creates VM-2 using explicit SA key on VM1
VM1_LAUNCH_VM2 = r"""#!/usr/bin/env python3
import json, time
import googleapiclient.discovery
import google.oauth2.service_account as service_account

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

def wait_zone_op(compute, project, zone, op_name):
    while True:
        op = compute.zoneOperations().get(project=project, zone=zone, operation=op_name).execute()
        if op.get("status") == "DONE":
            if "error" in op:
                raise RuntimeError(json.dumps(op["error"], indent=2))
            return
        time.sleep(2)

def main():
    cfg = json.load(open("/srv/vm1-config.json"))

    creds = service_account.Credentials.from_service_account_file(
        "/srv/service-credentials.json", scopes=SCOPES
    )
    compute = googleapiclient.discovery.build("compute", "v1", credentials=creds)

    img = compute.images().getFromFamily(
        project=cfg["image_project"], family=cfg["image_family"]
    ).execute()
    image_link = img["selfLink"]

    vm2_startup = open("/srv/vm2-startup-script.sh").read()

    body = {
        "name": cfg["vm2_name"],
        "machineType": f"zones/{cfg['zone']}/machineTypes/{cfg['vm2_machine_type']}",
        "networkInterfaces": [{
            "network": f"projects/{cfg['project']}/global/networks/default",
            "accessConfigs": [{"name": "External NAT", "type": "ONE_TO_ONE_NAT"}]
        }],
        "disks": [{
            "boot": True,
            "autoDelete": True,
            "initializeParams": {"sourceImage": image_link}
        }],
        "metadata": {"items": [{"key": "startup-script", "value": vm2_startup}]},
        # IMPORTANT: match Part1 firewall rule targetTags = allow-5000
        "tags": {"items": ["allow-5000"]},
    }

    op = compute.instances().insert(
        project=cfg["project"], zone=cfg["zone"], body=body
    ).execute()
    wait_zone_op(compute, cfg["project"], cfg["zone"], op["name"])
    print("VM-2 created:", cfg["vm2_name"])

if __name__ == "__main__":
    main()
"""


# VM-1 startup (runs automatically on VM1): pulls metadata files and runs launcher
VM1_STARTUP = r"""#!/bin/bash
set -eux

mkdir -p /srv
cd /srv

# pull files from instance metadata
curl -sf -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/service-credentials \
  -o service-credentials.json

curl -sf -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/vm2-startup-script \
  -o vm2-startup-script.sh

curl -sf -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/vm1-launch-vm2 \
  -o vm1-launch-vm2.py

curl -sf -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/vm1-config \
  -o vm1-config.json

# install python + venv, then install google API libs into venv
apt-get update -y
apt-get install -y python3 python3-venv

python3 -m venv /srv/venv
/srv/venv/bin/pip install --upgrade pip
/srv/venv/bin/pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib

# run VM2 launcher
/srv/venv/bin/python /srv/vm1-launch-vm2.py
"""


def wait_zone_op(compute, project, zone, op_name):
    while True:
        op = compute.zoneOperations().get(
            project=project, zone=zone, operation=op_name
        ).execute()
        if op.get("status") == "DONE":
            if "error" in op:
                raise RuntimeError(json.dumps(op["error"], indent=2))
            return
        time.sleep(2)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--instance", required=True, help="Name of VM-1 to create")
    p.add_argument("--zone", default=BASE_ZONE)
    args = p.parse_args()

    # Use explicit SA key locally to create VM1
    creds = service_account.Credentials.from_service_account_file(
        "service-credentials.json", scopes=SCOPES
    )
    project = os.getenv("GOOGLE_CLOUD_PROJECT") or creds.project_id
    compute = googleapiclient.discovery.build("compute", "v1", credentials=creds)

    vm1_cfg = {
        "project": project,
        "zone": args.zone,
        "vm2_name": f"{args.instance}-vm2",
        "vm2_machine_type": MACHINE_TYPE_VM2,
        "image_project": IMAGE_PROJECT,
        "image_family": IMAGE_FAMILY,
    }

    # image for VM1 itself
    image = compute.images().getFromFamily(
        project=IMAGE_PROJECT, family=IMAGE_FAMILY
    ).execute()
    image_link = image["selfLink"]

    body = {
        "name": args.instance,
        "machineType": f"zones/{args.zone}/machineTypes/{MACHINE_TYPE_VM1}",
        "networkInterfaces": [{
            "network": f"projects/{project}/global/networks/default",
            "accessConfigs": [{"name": "External NAT", "type": "ONE_TO_ONE_NAT"}]
        }],
        "disks": [{
            "boot": True,
            "autoDelete": True,
            "initializeParams": {"sourceImage": image_link}
        }],
        "metadata": {
            "items": [
                {"key": "startup-script", "value": VM1_STARTUP},
                # pass files into VM1 via metadata so VM1 can retrieve them and automatically do the rest
                {"key": "service-credentials", "value": open("service-credentials.json").read()},
                {"key": "vm2-startup-script", "value": VM2_STARTUP},
                {"key": "vm1-launch-vm2", "value": VM1_LAUNCH_VM2},
                {"key": "vm1-config", "value": json.dumps(vm1_cfg)},
            ]
        }
    }

    op = compute.instances().insert(project=project, zone=args.zone, body=body).execute()
    wait_zone_op(compute, project, args.zone, op["name"])
    print("VM-1 created:", args.instance, "-> VM-2:", vm1_cfg["vm2_name"])
    print("Next: wait for 1-2 minutes, then open VM-2 external IP on :5000")


if __name__ == "__main__":
    main()
