# -*- coding: utf-8 -*-
from __future__ import absolute_import

import time

import cherrypy

try:
    from ceph.deployment.drive_group import DriveGroupSpec, DriveGroupValidationError
except ImportError:
    pass

from . import ApiController, Endpoint, ReadPermission, UpdatePermission
from . import RESTController, Task
from .. import mgr
from ..exceptions import DashboardException
from ..security import Scope
from ..services.exception import handle_orchestrator_error
from ..services.orchestrator import OrchClient
from ..tools import TaskManager, wraps


def get_device_osd_map():
    """Get mappings from inventory devices to OSD IDs.

    :return: Returns a dictionary containing mappings. Note one device might
        shared between multiple OSDs.
        e.g. {
                 'node1': {
                     'nvme0n1': [0, 1],
                     'vdc': [0],
                     'vdb': [1]
                 },
                 'node2': {
                     'vdc': [2]
                 }
             }
    :rtype: dict
    """
    result = {}
    for osd_id, osd_metadata in mgr.get('osd_metadata').items():
        hostname = osd_metadata.get('hostname')
        devices = osd_metadata.get('devices')
        if not hostname or not devices:
            continue
        if hostname not in result:
            result[hostname] = {}
        # for OSD contains multiple devices, devices is in `sda,sdb`
        for device in devices.split(','):
            if device not in result[hostname]:
                result[hostname][device] = [int(osd_id)]
            else:
                result[hostname][device].append(int(osd_id))
    return result


def orchestrator_task(name, metadata, wait_for=2.0):
    return Task("orchestrator/{}".format(name), metadata, wait_for)


def raise_if_no_orchestrator(method):
    @wraps(method)
    def inner(self, *args, **kwargs):
        orch = OrchClient.instance()
        if not orch.available():
            raise cherrypy.HTTPError(503)
        return method(self, *args, **kwargs)
    return inner


@ApiController('/orchestrator')
class Orchestrator(RESTController):

    @Endpoint()
    @ReadPermission
    def status(self):
        return OrchClient.instance().status()

    @Endpoint(method='POST')
    @UpdatePermission
    @raise_if_no_orchestrator
    @handle_orchestrator_error('osd')
    @orchestrator_task('identify_device', ['{hostname}', '{device}'])
    def identify_device(self, hostname, device, duration):
        # type: (str, str, int) -> None
        """
        Identify a device by switching on the device light for N seconds.
        :param hostname: The hostname of the device to process.
        :param device: The device identifier to process, e.g. ``ABC1234DEF567-1R1234_ABC8DE0Q``.
        :param duration: The duration in seconds how long the LED should flash.
        """
        orch = OrchClient.instance()
        TaskManager.current_task().set_progress(0)
        orch.blink_device_light(hostname, device, 'ident', True)
        for i in range(int(duration)):
            percentage = int(round(i / float(duration) * 100))
            TaskManager.current_task().set_progress(percentage)
            time.sleep(1)
        orch.blink_device_light(hostname, device, 'ident', False)
        TaskManager.current_task().set_progress(100)


@ApiController('/orchestrator/inventory', Scope.HOSTS)
class OrchestratorInventory(RESTController):

    @raise_if_no_orchestrator
    def list(self, hostname=None):
        orch = OrchClient.instance()
        hosts = [hostname] if hostname else None
        inventory_nodes = [node.to_json() for node in orch.inventory.list(hosts)]
        device_osd_map = get_device_osd_map()
        for inventory_node in inventory_nodes:
            node_osds = device_osd_map.get(inventory_node['name'])
            for device in inventory_node['devices']:
                if node_osds:
                    device['osd_ids'] = sorted(node_osds.get(device['path'], []))
                else:
                    device['osd_ids'] = []
        return inventory_nodes


@ApiController('/orchestrator/service', Scope.HOSTS)
class OrchestratorService(RESTController):

    @raise_if_no_orchestrator
    def list(self, hostname=None):
        orch = OrchClient.instance()
        return [service.to_json() for service in orch.services.list(None, None, hostname)]


@ApiController('/orchestrator/osd', Scope.OSD)
class OrchestratorOsd(RESTController):

    @raise_if_no_orchestrator
    def create(self, drive_group):
        orch = OrchClient.instance()
        try:
            orch.osds.create(DriveGroupSpec.from_json(drive_group))
        except (ValueError, TypeError, DriveGroupValidationError) as e:
            raise DashboardException(e, component='osd')
