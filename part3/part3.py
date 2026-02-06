#!/usr/bin/env python3
import argparse
import os
import time

import googleapiclient.discovery
import google.oauth2.service_account as service_account
from googleapiclient.errors import HttpError

BASE_ZONE = "us-west1-b"
IMAGE_PROJECT = "debian-cloud"
IMAGE_FAMILY = "debian-12"
MACHINE_TYPE_VM1 = "e2-medium"
MACHINE_TYPE_VM2 = "e2-medium"

# ---------------- VM-2 startup script (runs Flask) ----------------
VM2_STARTUP = r"""#!/bin/bash
set -eux
apt-get update -y
apt-get install -y python3 python3-pip
pip3 install --upgrade pip
pip3 install flask

cat >/srv/app.py <<'PY'
from flask import Flask
app = Flask(__name__)
@app.get("/")
def hi():
    return "Hello from VM-2!\n"
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
PY

nohup python3 /srv/app.py >/var/log/flask.log 2>&1 &
"""

# ---------------- VM-1 code that launches VM-2 ----------------
VM1_LAUNCH_VM2 = r"""#!/usr/bin/env python3
import json, time
import googleapiclient.discovery
import google.oauth2.service_account as service_account

ZONE = None
PROJECT = None
VM2_NAME = None

def wait_zone_op(compute, project, zone, op_name):
    while True:
        op = compute.zoneOperations().get(project=project, zone=zone, operation=op_name).execute()
        if op.get("status") == "DONE":
            if "error" in op:
                raise RuntimeError(json.dumps(op["error"], indent=2))
            return
        time.sleep(2)

def main():
    with open("/srv/vm1-config.json", "r") as f:
        cfg = json.load(f)

    global ZONE, PROJECT, VM2_NAME
    ZONE = cfg["zone"]
    PROJECT = cfg["project"]
    VM2_NAME = cfg["vm2_name"]

    creds = service_account.Credentials.from_service_account_file(
        "/srv/service-credentials.json",
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    compute = googleapiclient.discovery.build("compute", "v1", credentials=creds)

    img = compute.images().getFromFamily(project=cfg["image_project"], family=cfg["image_family"]).execute()
    image_link = img["selfLink"]

    with open("/srv/vm2-startup-script.sh", "r") as f:
        vm2_startup = f.read()

    body = {
        "name": VM2_NAME,
        "machineType": f"zones/{ZONE}/machineTypes/{cfg['vm2_machine_type']}",
        "networkInterfaces": [{
            "network": f"projects/{PROJECT}/global/networks/default",
            "accessConfigs": [{"name": "External NAT", "type": "ONE_TO_ONE_NAT"}]
        }],
        "disks": [{
            "boot": True,
            "autoDelete": True,
            "initializeParams": {"sourceImage": image_link}
        }],
        "metadata": {"items": [{"key": "startup-script", "value": vm2_startup}]},
        "tags": {"items": ["flask"]}
    }

    op = compute.instances().insert(project=PROJECT, zone=ZONE, body=body).execute()
    wait_zone_op(compute, PROJECT, ZONE, op["name"])
    print("VM-2 created:", VM2_NAME)

if __name__ == "__main__":
    main()
"""

# ---------------- VM-1 startup script (downloads metadata and runs launcher) ----------------
VM1_STARTUP = r"""#!/bin/bash
set -eux

mkdir -p /srv
cd /srv

curl -sf http://metadata.google.internal/computeMetadata/v1/instance/attributes/service-credentials \
  -H "Metadata-Flavor: Google" > service-credentials.json

curl -sf http://metadata.google.internal/computeMetadata/v1/instance/attributes/vm2-startup-script \
  -H "Metadata-Flavor: Google" > vm2-startup-script.sh

curl -sf http://metadata.google.internal/computeMetadata/v1/instance/attributes/vm1-launch-vm2 \
  -H "Metadata-Flavor: Google" > vm1-launch-vm2.py

curl -sf http://metadata.google.internal/computeMetadata/v1/instance/attributes/vm1-config \
  -H "Metadata-Flavor: Google" > vm1-config.json

apt-get update -y
apt-get install -y python3 python3-pip
pip3 install --upgrade google-api-python-client google-auth-httplib2 google-auth-oauthlib

python3 /srv/vm1-launch-vm2.py
"""


def wait_zone_op(compute, project, zone, op_name):
    while True:
        op = compute.zoneOperations().get(project=project, zone=zone, operation=op_name).execute()
        if op.get("status") == "DONE":
            if "error" in op:
                raise RuntimeError(str(op["error"]))
            return
        time.sleep(2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", required=True, help="VM-1 name")
    parser.add_argument("--zone", default=BASE_ZONE)
    args = parser.parse_args()

    # Auth from local service-credentials.json (explicit key)
    credentials = service_account.Credentials.from_service_account_file(
        filename="service-credentials.json",
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )

    project = os.getenv("GOOGLE_CLOUD_PROJECT") or credentials.project_id or "FILL_IN_YOUR_PROJECT"
    service = googleapiclient.discovery.build("compute", "v1", credentials=credentials)

    # VM-1 metadata includes: startup-script + files for VM-1 to download
    vm1_cfg = {
        "project": project,
        "zone": args.zone,
        "vm2_name": f"{args.instance}-vm2",
        "vm2_machine_type": MACHINE_TYPE_VM2,
        "image_project": IMAGE_PROJECT,
        "image_family": IMAGE_FAMILY,
    }

    image = service.images().getFromFamily(project=IMAGE_PROJECT, family=IMAGE_FAMILY).execute()
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
                {"key": "service-credentials", "value": open("service-credentials.json", "r").read()},
                {"key": "vm2-startup-script", "value": VM2_STARTUP},
                {"key": "vm1-launch-vm2", "value": VM1_LAUNCH_VM2},
                {"key": "vm1-config", "value": __import__("json").dumps(vm1_cfg)},
            ]
        }
    }

    op = service.instances().insert(project=project, zone=args.zone, body=body).execute()
    wait_zone_op(service, project, args.zone, op["name"])
    print("VM-1 created:", args.instance, "-> will create VM-2:", vm1_cfg["vm2_name"])


if __name__ == "__main__":
    main()
