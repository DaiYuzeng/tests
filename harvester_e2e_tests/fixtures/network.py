# Copyright (c) 2021 SUSE LLC
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of version 3 of the GNU General Public License as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.   See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, contact SUSE LLC.
#
# To contact SUSE about this file by physical or electronic mail,
# you may find current contact information at www.suse.com

from harvester_e2e_tests import utils
from pkg_resources import parse_version
import json
import polling2
import pytest


pytest_plugins = [
    'harvester_e2e_tests.fixtures.api_endpoints',
    'harvester_e2e_tests.fixtures.session',
    "harvester_e2e_tests.fixtures.api_client"
]


@pytest.fixture(scope='session')
def enable_vlan(request, admin_session, harvester_api_endpoints, api_client):
    vlan_nic = request.config.getoption('--vlan-nic')

    if api_client.cluster_version > parse_version("v1.0.3"):
        yield cluster_network(api_client, vlan_nic)
        if not request.config.getoption('--do-not-cleanup'):
            cluster_network(api_client, vlan_nic, delete=True)
        return

    resp = admin_session.get(harvester_api_endpoints.get_vlan)
    assert resp.status_code == 200, 'Failed to get vlan: %s' % (resp.content)
    vlan_json = resp.json()
    if 'config' not in vlan_json:
        vlan_json['config'] = {}
    if 'defaultPhysicalNIC' not in vlan_json['config']:
        vlan_json['config']['defaultPhysicalNIC'] = None

    if utils.is_marker_enabled(request, 'terraform'):
        utils.create_clusternetworks_terraform(
            request,
            admin_session,
            harvester_api_endpoints,
            'resource_clusternetworks',
            vlan_nic)
    else:
        vlan_json['enable'] = True
        vlan_json['config']['defaultPhysicalNIC'] = vlan_nic
        utils.poll_for_update_resource(request, admin_session,
                                       harvester_api_endpoints.update_vlan,
                                       vlan_json,
                                       harvester_api_endpoints.get_vlan)


def _cleanup_network(admin_session, harvester_api_endpoints, network_id,
                     wait_timeout, api_client):

    def _delete_network():
        if api_client.cluster_version > parse_version("v1.0.3"):
            resp = api_client.networks.delete(network_id, raw=True)
        else:
            resp = admin_session.delete(harvester_api_endpoints.delete_network % (network_id))
        if resp.status_code in [200, 204]:
            return True
        elif resp.status_code == 400:
            return False
        else:
            assert False, 'Failed to cleanup network %s: %s' % (
                network_id, resp.content)

    # NOTE(gyee): there's no way we know how many VMs the network is currently
    # attached to. Will need to keep trying till all the VMs had been deleted
    try:
        polling2.poll(
            _delete_network,
            step=5,
            timeout=wait_timeout)
    except polling2.TimeoutException as e:
        errmsg = 'Unable to cleanup network: %s' % (network_id)
        raise AssertionError(errmsg) from e


def _lookup_network(request, admin_session, harvester_api_endpoints, vlan_id):
    resp = admin_session.get(harvester_api_endpoints.list_networks)
    if resp.status_code == 200:
        for network in resp.json()['data']:
            if json.loads(network['spec']['config'])['vlan'] == vlan_id:
                return network
    return None


def _create_network(request, admin_session, harvester_api_endpoints, vlan_id, api_client):
    # NOTE(gyee): will name the network with the following convention as
    # VLAN ID must be unique. vlan_network_<VLAN ID>
    network_name = f'vlan-network-{vlan_id}'

    # If a network with the same VLAN ID already exist, just use it.
    network_data = _lookup_network(request, admin_session,
                                   harvester_api_endpoints, vlan_id)
    if network_data:
        return network_data

    if api_client.cluster_version > parse_version("v1.0.3"):
        vlan_nic = request.config.getoption('--vlan-nic')
        _, data = api_client.networks.create(network_name, vlan_id, cluster_network=vlan_nic)
        data['id'] = data['metadata']['name']
        return data

    request_json = utils.get_json_object_from_template(
        'basic_network',
        name=network_name,
        vlan=vlan_id
    )
    resp = admin_session.post(harvester_api_endpoints.create_network,
                              json=request_json)
    assert resp.status_code == 201, 'Unable to create a network: %s' % (
        resp.content)
    network_data = resp.json()
    utils.poll_for_resource_ready(request, admin_session,
                                  network_data['links']['view'])
    return network_data


@pytest.fixture(scope='session')
def network(request, admin_session, harvester_api_endpoints, enable_vlan, api_client):
    vlan_id = request.config.getoption('--vlan-id')
    # don't create network if VLAN is not correctly specified
    if vlan_id == -1:
        return

    network_data = _create_network(request, admin_session,
                                   harvester_api_endpoints, vlan_id, api_client)
    yield network_data

    if not request.config.getoption('--do-not-cleanup'):
        # XXX: we would need to check the network not be deleted terraform yet
        if not utils.is_marker_enabled(request, 'terraform') and \
            _lookup_network(request, admin_session, harvester_api_endpoints,
                            vlan_id) is not None:
            _cleanup_network(admin_session, harvester_api_endpoints,
                             network_data['id'],
                             request.config.getoption('--wait-timeout'), api_client)


@pytest.fixture(scope='class')
def bogus_network(request, admin_session, harvester_api_endpoints,
                  enable_vlan, api_client):
    vlan_id = request.config.getoption('--vlan-id')
    # don't create network if VLAN is not correctly specified
    if vlan_id == -1:
        return
    # change the VLAN ID to an invalid one
    vlan_id += 1

    network_data = _create_network(request, admin_session,
                                   harvester_api_endpoints, vlan_id, api_client)
    yield network_data

    if not request.config.getoption('--do-not-cleanup'):
        _cleanup_network(admin_session, harvester_api_endpoints,
                         network_data['id'],
                         request.config.getoption('--wait-timeout'), api_client)


# This fixture is only called by test_create_edit_network
# in apis/test_networks.py.
# vlan_id is set to vlan_id + 1
@pytest.fixture(scope='class')
def network_for_update_test(request, admin_session,
                            harvester_api_endpoints, enable_vlan, api_client):
    vlan_id = request.config.getoption('--vlan-id')
    # don't create network if VLAN is not correctly specified
    if vlan_id == -1:
        return

    request_json = utils.get_json_object_from_template(
        'basic_network',
        vlan=vlan_id + 1
    )
    resp = admin_session.post(harvester_api_endpoints.create_network,
                              json=request_json)
    assert resp.status_code == 201, 'Unable to create a network: %s' % (
        resp.content)
    network_data = resp.json()
    utils.poll_for_resource_ready(request, admin_session,
                                  network_data['links']['view'])
    yield network_data

    if not request.config.getoption('--do-not-cleanup'):
        _cleanup_network(admin_session, harvester_api_endpoints,
                         network_data['id'],
                         request.config.getoption('--wait-timeout'), api_client)


@pytest.fixture(scope='class')
def network_using_terraform(request, admin_session,
                            harvester_api_endpoints, enable_vlan):
    vlan_id = request.config.getoption('--vlan-id')
    # don't create network if VLAN is not correctly specified
    if vlan_id == -1:
        return

    # If a network with the same VLAN ID already exist,
    # don't try to create but import it
    network_data = _lookup_network(request, admin_session,
                                   harvester_api_endpoints, vlan_id)

    if network_data:
        import_flag = True
    else:
        import_flag = False

    network_json = utils.create_network_terraform(request, admin_session,
                                                  harvester_api_endpoints,
                                                  'resource_network',
                                                  vlan_id, import_flag)
    yield network_json

    if not request.config.getoption('--do-not-cleanup') and not import_flag:
        utils.destroy_resource(
            request,
            admin_session,
            'harvester_network.' + network_json['metadata']['name'])


def cluster_network(api_client, nic_name, delete=False):
    if delete:
        api_client.clusternetworks.delete_config(nic_name)
        api_client.clusternetworks.delete(nic_name)
    else:
        api_client.clusternetworks.create(nic_name)
        api_client.clusternetworks.create_config(nic_name, nic_name, nic_name)
