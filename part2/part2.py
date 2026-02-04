#!/usr/bin/env python3
import argparse
import time
import re

import googleapiclient.discovery
import google.auth
from googleapiclient.errors import HttpError

credentials, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
service = googleapiclient.discovery.build("compute", "v1", credentials=credentials)

BASE_ZONE = "us-west1-b"
CLONE_ZONES = ["us-west1-a", "us-west1-c", "us-west1-b"]  # try b last (stockout)
MACHINE_TYPE = "e2-medium"
COUNT = 3


def list_instances(compute, project, zone):
    result = compute.instances().list(project=project, zone=zone).execute()
    return result["items"] if "items" in result else []


def wait_zone_op(op_name, zone):
    while True:
        op = service.zoneOperations().get(project=project, zone=zone, operation=op_name).execute()
        if op["status"] == "DONE":
            if "error" in op:
                raise RuntimeError(op["error"])
            return
        time.sleep(2)


def wait_running(zone, instance_name):
    while True:
        inst = service.instances().get(project=project, zone=zone, instance=instance_name).execute()
        if inst["status"] == "RUNNING":
            return
        time.sleep(2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", required=True, help="Part 1 instance name (e.g., flask-vm)")
    args = parser.parse_args()
    base_instance = args.instance
    snapshot_name = f"base-snapshot-{base_instance}"

    print("Your running instances are:")
    for inst in list_instances(service, project, BASE_ZONE):
        print(inst["name"])

    # ---- 1) Create snapshot from the Part 1 instance boot disk (disk interface: disks.createSnapshot) ----
    inst = service.instances().get(project=project, zone=BASE_ZONE, instance=base_instance).execute()
    boot_disk_url = inst["disks"][0]["source"]
    disk_name = re.search(r"/disks/([^/]+)$", boot_disk_url).group(1)

    try:
        snap_link = service.snapshots().get(project=project, snapshot=snapshot_name).execute()["selfLink"]
        print(f"Snapshot already exists: {snapshot_name}")
    except HttpError as e:
        if e.resp.status != 404:
            raise
        print(f"Creating snapshot: {snapshot_name} (from disk {disk_name})")
        op = service.disks().createSnapshot(
            project=project, zone=BASE_ZONE, disk=disk_name, body={"name": snapshot_name}
        ).execute()
        wait_zone_op(op["name"], BASE_ZONE)
        snap_link = service.snapshots().get(project=project, snapshot=snapshot_name).execute()["selfLink"]

    # ---- 2) Create three instances from snapshot + measure time to RUNNING ----
    results = []
    for i in range(1, COUNT + 1):
        clone_name = f"{base_instance}-clone-{i}"

        last_err = None
        for z in CLONE_ZONES:
            t0 = time.time()
            try:
                body = {
                    "name": clone_name,
                    "machineType": f"zones/{z}/machineTypes/{MACHINE_TYPE}",
                    "disks": [{
                        "boot": True,
                        "autoDelete": True,
                        "initializeParams": {"sourceSnapshot": snap_link},
                    }],
                    "networkInterfaces": [{
                        "network": "global/networks/default",
                        "accessConfigs": [{"name": "External NAT", "type": "ONE_TO_ONE_NAT"}],
                    }],
                }

                op = service.instances().insert(project=project, zone=z, body=body).execute()
                wait_zone_op(op["name"], z)
                wait_running(z, clone_name)

                dt = time.time() - t0
                print(f"{clone_name} in {z}: {dt:.2f} seconds")
                results.append((clone_name, z, dt))
                last_err = None
                break
            except Exception as e:
                last_err = e

        if last_err:
            raise RuntimeError(f"Failed to create {clone_name} in all zones. Last error: {last_err}")

    # ---- 3) Write TIMING.md ----
    with open("TIMING.md", "w", encoding="utf-8") as f:
        f.write("# VM Clone Timing\n\n")
        f.write(f"Base instance: `{base_instance}`  \n")
        f.write(f"Snapshot: `{snapshot_name}`  \n\n")
        f.write("| Instance Name | Zone | Time (seconds) |\n")
        f.write("|---|---|---:|\n")
        for name, z, dt in results:
            f.write(f"| {name} | {z} | {dt:.2f} |\n")

    print("Wrote TIMING.md")


if __name__ == "__main__":
    main()
